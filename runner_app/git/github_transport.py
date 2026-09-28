"""Owner-process HTTPS Git transport for one exact GitHub repository.

Only fixed-URL bare clone, normal `ls-remote`, tracking-ref fetch, and exact-commit
`push` are exposed. Authentication uses
Git askpass with anonymous memory file descriptors; the token is never placed in
argv, the child environment, repository config, or a persistent temporary file.
The orchestrator must verify the repository's numeric ID with GitHub REST and
journal push intent before calling this transport.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from runner_app.adapters.github import GitHubRepository
from runner_app.git.argv import _BASE_CONFIG, GitError, _environment, run_git, validate_branch
from runner_app.git.registry import SourceRegistry
from runner_app.git.worktrees import (
    _check_tree_features,
    _inspect_config,
    _reject_object_alternates,
    _repo,
)

_LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}\Z", re.ASCII)
_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z", re.ASCII)
_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z", re.ASCII)
_MARKER = re.compile(r"cdr:v1:[0-9a-f]{64}\Z", re.ASCII)
_MAX_OUTPUT = 1_048_576
_MAX_HEADS = 256
FETCH_REFSPEC = "+refs/heads/*:refs/remotes/origin/*"
_ASKPASS = b'''#!/usr/bin/python3
import os
import re
import sys

prompt = sys.argv[1] if len(sys.argv) == 2 else ""
if re.fullmatch(r"Username for 'https://github\\.com(?:/[^']*)?': ?", prompt):
    os.write(1, b"x-access-token\\n")
elif re.fullmatch(r"Password for 'https://x-access-token@github\\.com(?:/[^']*)?': ?", prompt):
    try:
        fd = int(os.environ["CDR_GITHUB_TOKEN_FD"])
        os.lseek(fd, 0, os.SEEK_SET)
        token = os.read(fd, 8193)
        if not 1 <= len(token) <= 8192 or any(c < 33 or c > 126 for c in token):
            sys.exit(1)
        os.write(1, token + b"\\n")
    except (OSError, ValueError, KeyError):
        sys.exit(1)
else:
    sys.exit(1)
'''


def _validate_token(token: str) -> bytes:
    if not isinstance(token, str):
        raise GitError("GITHUB_AUTH_NOT_CONFIGURED", "GitHub token is unavailable")
    try:
        raw = token.encode("ascii", errors="strict")
    except UnicodeError:
        raise GitError("GITHUB_AUTH_NOT_CONFIGURED", "GitHub token is invalid") from None
    if not 1 <= len(raw) <= 8192 or any(ch < 33 or ch > 126 for ch in raw):
        raise GitError("GITHUB_AUTH_NOT_CONFIGURED", "GitHub token is invalid")
    return raw


class OwnerTokenFile:
    """Read a locally configured token file; construction does not read it.

    The immediate directory must be owner-only (0700), and the regular token file
    must be owned by this process's user and have mode 0600. Symlinks are refused.
    Configure its absolute path outside all source and implementation repositories.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        if not self.path.is_absolute() or self.path.name in {"", ".", ".."}:
            raise ValueError("An absolute owner-only GitHub token path is required")

    def __call__(self) -> str:
        try:
            directory_fd = os.open(
                self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            )
            try:
                directory = os.fstat(directory_fd)
                if (directory.st_uid != os.geteuid() or not stat.S_ISDIR(directory.st_mode)
                        or stat.S_IMODE(directory.st_mode) != 0o700):
                    raise GitError("GITHUB_AUTH_UNSAFE", "GitHub token directory is not owner-only")
                token_fd = os.open(
                    self.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=directory_fd,
                )
                try:
                    token_file = os.fstat(token_fd)
                    if (token_file.st_uid != os.geteuid() or not stat.S_ISREG(token_file.st_mode)
                            or stat.S_IMODE(token_file.st_mode) != 0o600):
                        raise GitError("GITHUB_AUTH_UNSAFE", "GitHub token file is not owner-only")
                    raw = os.read(token_fd, 8194)
                finally:
                    os.close(token_fd)
            finally:
                os.close(directory_fd)
        except GitError:
            raise
        except OSError:
            raise GitError("GITHUB_AUTH_NOT_CONFIGURED", "GitHub token file is unavailable") from None
        if raw.endswith(b"\n"):
            raw = raw[:-1]
        try:
            token = raw.decode("ascii", errors="strict")
        except UnicodeError:
            raise GitError("GITHUB_AUTH_NOT_CONFIGURED", "GitHub token is invalid") from None
        _validate_token(token)
        return token


@contextmanager
def _credential_fds(token: bytes) -> Iterator[tuple[int, int]]:
    if not hasattr(os, "memfd_create"):
        raise GitError("GITHUB_GIT_UNAVAILABLE", "Anonymous credential transport is unavailable")
    script_fd = -1
    token_fd = -1
    try:
        script_fd = os.memfd_create("cdr-askpass", flags=os.MFD_CLOEXEC)
        token_fd = os.memfd_create("cdr-git-credential", flags=os.MFD_CLOEXEC)
        os.write(script_fd, _ASKPASS)
        os.write(token_fd, token)
        os.lseek(script_fd, 0, os.SEEK_SET)
        os.lseek(token_fd, 0, os.SEEK_SET)
        yield script_fd, token_fd
    except OSError:
        raise GitError("GITHUB_GIT_UNAVAILABLE", "Anonymous credential transport failed") from None
    finally:
        if token_fd >= 0:
            try:
                os.ftruncate(token_fd, 0)
            except OSError:
                pass
            os.close(token_fd)
        if script_fd >= 0:
            os.close(script_fd)


def _command(operation: Sequence[str]) -> list[str]:
    """Start from the canonical local Git safety config, opening only HTTPS."""
    command = ["git"]
    for setting in _BASE_CONFIG:
        command.extend(("-c", setting))
    for setting in (
        "protocol.file.allow=never",
        "protocol.https.allow=always",
        "credential.helper=",
        "credential.username=x-access-token",
        "credential.useHttpPath=true",
        "http.followRedirects=false",
        "http.sslVerify=true",
        "http.proxy=",
        "http.extraHeader=",
        "push.recurseSubmodules=no",
    ):
        command.extend(("-c", setting))
    command.extend(operation)
    return command


def _child_environment(script_fd: int, token_fd: int) -> dict[str, str]:
    env = _environment()
    env["GIT_ASKPASS"] = f"/proc/self/fd/{script_fd}"
    env["CDR_GITHUB_TOKEN_FD"] = str(token_fd)
    return env


def _reject_network_config(repo: Path) -> None:
    """Refuse local settings that could route HTTPS or the credential elsewhere."""
    _inspect_config(repo)
    _reject_object_alternates(repo)
    raw = run_git(repo, ["config", "--no-includes", "--local", "--null", "--list"])
    for entry in raw.split("\x00"):
        if not entry:
            continue
        key, separator, _ = entry.partition("\n")
        if not separator:
            raise GitError("UNSAFE_GIT_CONFIG", "Repository configuration is malformed")
        key = key.casefold()
        if (key.startswith(("http.", "https.", "credential.", "url.", "include.",
                           "includeif.", "filter.", "proxy."))
                or key in {"core.askpass", "core.gitproxy", "core.sshcommand"}):
            raise GitError("UNSAFE_GIT_CONFIG", "Repository has unsafe network configuration")


def _run_network_git(repo: Path, operation: Sequence[str], token: bytes,
                     *, timeout: float) -> bytes:
    if not 0 < timeout <= 300:
        raise GitError("INVALID_GIT_TIMEOUT", "Git timeout is outside the supported range")
    if any(not isinstance(arg, str) or "\x00" in arg or len(arg.encode()) > 4096
           for arg in operation):
        raise GitError("INVALID_GIT_ARGV", "Network Git argument is invalid")
    command = _command(operation)
    output = bytearray()
    error_output = bytearray()
    with _credential_fds(token) as (script_fd, token_fd):
        try:
            process = subprocess.Popen(
                command, cwd=repo, env=_child_environment(script_fd, token_fd),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True, umask=0o077, pass_fds=(script_fd, token_fd),
            )
        except OSError:
            raise GitError("GITHUB_GIT_START_FAILED", "Unable to start GitHub Git transport") from None
        deadline = time.monotonic() + timeout
        try:
            assert process.stdout is not None and process.stderr is not None
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, output)
                selector.register(process.stderr, selectors.EVENT_READ, error_output)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise GitError("GITHUB_GIT_TIMEOUT", "GitHub Git operation timed out")
                    for key, _ in selector.select(min(remaining, 0.25)):
                        data = os.read(key.fd, 65536)
                        if not data:
                            selector.unregister(key.fileobj)
                            continue
                        key.data.extend(data)
                        if len(output) + len(error_output) > _MAX_OUTPUT:
                            raise GitError("GITHUB_GIT_OUTPUT_LIMIT", "GitHub Git output exceeded limit")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GitError("GITHUB_GIT_TIMEOUT", "GitHub Git operation timed out")
            try:
                status = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                raise GitError("GITHUB_GIT_TIMEOUT", "GitHub Git operation timed out") from None
            if status != 0:
                raise GitError("GITHUB_GIT_FAILED", "GitHub Git operation failed")
            return bytes(output)
        except BaseException:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            raise
        finally:
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()


class RepositoryInspector(Protocol):
    def get_repository(self, owner: str, name: str, *,
                       expected_repo_id: int | None = None) -> GitHubRepository: ...


@dataclass(frozen=True)
class BaselineResult:
    repository_id: int
    commit: str
    disposition: Literal["created", "attached", "reconciled"]


def read_tracking_refs(repo: Path) -> dict[str, str]:
    """Read only local origin tracking refs from a managed bare repository."""
    checked = _repo(repo)
    _reject_network_config(checked)
    if run_git(checked, ["rev-parse", "--is-bare-repository"]).strip() != "true":
        raise GitError("INVALID_REPOSITORY", "Fetch requires a managed bare repository")
    raw = run_git(checked, [
        "for-each-ref", "--format=%(objectname)%00%(refname)%00%(symref)%00",
        "refs/remotes/origin/",
    ])
    heads: dict[str, str] = {}
    for line in raw.splitlines():
        fields = line.split("\x00")
        if len(fields) != 4 or fields[-1]:
            raise GitError("GIT_TRACKING_REFS_INVALID", "Tracking ref read is malformed")
        oid, ref, symbolic, _ = fields
        if symbolic:
            continue  # origin/HEAD is a local symbolic alias, not a fetched branch.
        if not ref.startswith("refs/remotes/origin/") or not _OID.fullmatch(oid):
            raise GitError("GIT_TRACKING_REFS_INVALID", "Tracking ref identity is invalid")
        branch = validate_branch(ref.removeprefix("refs/remotes/origin/"))
        if branch in heads or len(heads) >= _MAX_HEADS:
            raise GitError("GIT_TRACKING_REFS_INVALID", "Tracking ref set is ambiguous or too large")
        heads[branch] = oid
    return dict(sorted(heads.items()))


def tracking_refs_digest(heads: dict[str, str]) -> str:
    """Stable local effect identity; never a claim that GitHub was contacted."""
    return hashlib.sha256(json.dumps(heads, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class GitHubGitTransport:
    """Direct-URL bare clone, Git reads and non-force pushes for one verified repository."""

    def __init__(self, repository: GitHubRepository,
                 token_provider: Callable[[], str]) -> None:
        if not callable(token_provider):
            raise TypeError("An owner-only GitHub token provider is required")
        if (not _LOGIN.fullmatch(repository.owner) or not _REPO.fullmatch(repository.name)
                or repository.private is not True or repository.id <= 0
                or repository.owner_id <= 0 or not repository.node_id
                or repository.full_name != f"{repository.owner}/{repository.name}"
                or repository.html_url != f"https://github.com/{repository.owner}/{repository.name}"
                or repository.clone_url != f"https://github.com/{repository.owner}/{repository.name}.git"
                or repository.ssh_url != f"git@github.com:{repository.owner}/{repository.name}.git"):
            raise GitError("GITHUB_REPOSITORY_UNAPPROVED", "Exact private GitHub repository is required")
        self.repository = repository
        self._token_provider = token_provider
        self.url = repository.clone_url

    def _prepared_repo(self, repo: Path) -> Path:
        checked = _repo(repo)
        if isinstance(self._token_provider, OwnerTokenFile):
            credential = self._token_provider.path.resolve()
            implementation = Path(__file__).resolve().parents[2]
            for forbidden in (checked.parent.resolve(), implementation):
                try:
                    credential.relative_to(forbidden)
                except ValueError:
                    continue
                raise GitError("GITHUB_AUTH_UNSAFE", "GitHub token cannot be inside a repository")
        _reject_network_config(checked)
        return checked

    def _token(self) -> bytes:
        try:
            return _validate_token(self._token_provider())
        except GitError:
            raise
        except Exception:  # noqa: BLE001 - provider exceptions may contain credential bytes
            raise GitError("GITHUB_AUTH_NOT_CONFIGURED", "GitHub token is unavailable") from None

    def clone_bare(self, stage_parent: Path, destination: Path) -> None:
        """Clone this exact private repository into a new, journaled private stage.

        The caller must first persist the import intent and verify the current REST
        repository identity. A bare clone never checks out source files or invokes
        submodules or smudge filters. The caller inspects the result before promotion.
        """
        stage_parent = Path(stage_parent)
        destination = Path(destination)
        if (not stage_parent.is_absolute() or ".." in stage_parent.parts
                or stage_parent.is_symlink() or not stage_parent.is_dir()
                or not stage_parent.name.startswith(".github-import-")
                or destination != stage_parent / "repo.git"
                or destination.exists() or destination.is_symlink()):
            raise GitError("INVALID_IMPORT_STAGE", "A new managed GitHub import stage is required")
        stage_info = stage_parent.stat()
        if stage_info.st_uid != os.geteuid() or stat.S_IMODE(stage_info.st_mode) != 0o700:
            raise GitError("INVALID_IMPORT_STAGE", "GitHub import stage must be owner-only")
        if isinstance(self._token_provider, OwnerTokenFile):
            credential = self._token_provider.path.resolve()
            implementation = Path(__file__).resolve().parents[2]
            for forbidden in (stage_parent.parent.resolve(), implementation):
                try:
                    credential.relative_to(forbidden)
                except ValueError:
                    continue
                raise GitError("GITHUB_AUTH_UNSAFE", "GitHub token cannot be inside a repository")
        _run_network_git(
            stage_parent,
            ["clone", "--bare", "--no-local", "--no-hardlinks", "--no-tags",
             self.url, str(destination)],
            self._token(), timeout=300,
        )

    def read_head(self, repo: Path, branch: str) -> str | None:
        """Read one exact remote heads ref; no fetch or local repository mutation."""
        checked = self._prepared_repo(repo)
        branch = validate_branch(branch)
        ref = f"refs/heads/{branch}"
        raw = _run_network_git(
            checked, ["ls-remote", "--refs", "--heads", self.url, ref],
            self._token(), timeout=30,
        )
        if not raw:
            return None
        lines = raw.splitlines()
        if len(lines) != 1:
            raise GitError("GITHUB_GIT_RESPONSE_INVALID", "Remote head response is ambiguous")
        try:
            oid, returned_ref = lines[0].decode("ascii").split("\t", 1)
        except (UnicodeError, ValueError):
            raise GitError("GITHUB_GIT_RESPONSE_INVALID", "Remote head response is invalid") from None
        if returned_ref != ref or not _OID.fullmatch(oid):
            raise GitError("GITHUB_GIT_RESPONSE_INVALID", "Remote head identity differs")
        return oid

    def list_heads(self, repo: Path) -> dict[str, str]:
        """Advertise exact private GitHub heads before a journaled fetch intent."""
        checked = self._prepared_repo(repo)
        raw = _run_network_git(
            checked, ["ls-remote", "--refs", "--heads", self.url],
            self._token(), timeout=30,
        )
        heads: dict[str, str] = {}
        for line in raw.splitlines():
            try:
                oid, ref = line.decode("ascii").split("\t", 1)
            except (UnicodeError, ValueError):
                raise GitError("GITHUB_GIT_RESPONSE_INVALID", "Remote heads response is invalid") from None
            if not ref.startswith("refs/heads/") or not _OID.fullmatch(oid):
                raise GitError("GITHUB_GIT_RESPONSE_INVALID", "Remote head identity is invalid")
            branch = validate_branch(ref.removeprefix("refs/heads/"))
            if branch in heads or len(heads) >= _MAX_HEADS:
                raise GitError("GITHUB_GIT_RESPONSE_INVALID", "Remote head set is ambiguous or too large")
            heads[branch] = oid
        return dict(sorted(heads.items()))

    def fetch_heads(self, repo: Path) -> dict[str, str]:
        """Fetch only GitHub heads into local origin tracking refs, never a worktree."""
        checked = self._prepared_repo(repo)
        read_tracking_refs(checked)  # Refuse non-bare or unsafe existing local state first.
        _run_network_git(
            checked,
            ["fetch", "--atomic", "--no-tags", "--no-recurse-submodules",
             "--no-auto-gc", "--no-write-fetch-head", "--no-progress",
             self.url, FETCH_REFSPEC],
            self._token(), timeout=300,
        )
        return read_tracking_refs(checked)

    def push_commit(self, repo: Path, commit: str, branch: str) -> None:
        """Push an exact local commit to a nondefault branch with normal FF rules."""
        checked = self._prepared_repo(repo)
        if not isinstance(commit, str) or not _OID.fullmatch(commit):
            raise GitError("INVALID_COMMIT", "A full local commit ID is required")
        branch = validate_branch(branch)
        if self.repository.default_branch is None or branch == self.repository.default_branch:
            raise GitError("PROTECTED_BRANCH", "Default branch push needs a separate bootstrap policy")
        if run_git(checked, ["rev-parse", "--verify", f"{commit}^{{commit}}"] ).strip() != commit:
            raise GitError("INVALID_COMMIT", "Exact local commit is unavailable")
        ref = f"refs/heads/{branch}"
        _run_network_git(
            checked,
            ["push", "--porcelain", "--no-verify", "--no-follow-tags", "--recurse-submodules=no",
             self.url, f"{commit}:{ref}"],
            self._token(), timeout=120,
        )


    def _confirmed_private(self, inspector: RepositoryInspector,
                           provision_marker: str) -> None:
        """Check current REST identity before the exceptional default-branch operation."""
        if (not isinstance(provision_marker, str) or not _MARKER.fullmatch(provision_marker)
                or self.repository.description != provision_marker):
            raise GitError("GITHUB_REPOSITORY_UNVERIFIED",
                           "Persisted private repository provision marker differs")
        try:
            observed = inspector.get_repository(
                self.repository.owner, self.repository.name,
                expected_repo_id=self.repository.id,
            )
        except Exception:  # noqa: BLE001 - provider details may contain credentials
            raise GitError("GITHUB_REPOSITORY_UNVERIFIED",
                           "GitHub repository identity could not be confirmed") from None
        if (observed.id != self.repository.id or observed.node_id != self.repository.node_id
                or observed.owner_id != self.repository.owner_id
                or observed.full_name != self.repository.full_name
                or observed.clone_url != self.url or observed.private is not True
                or observed.description != provision_marker):
            raise GitError("GITHUB_REPOSITORY_UNVERIFIED",
                           "GitHub repository identity changed")
        if self.repository.default_branch != "main" or observed.default_branch != "main":
            raise GitError("DEFAULT_BRANCH_UNSUPPORTED",
                           "Initial publication requires an explicitly confirmed main default branch")

    @staticmethod
    def _root_commit(repo: Path, commit: str) -> None:
        if not isinstance(commit, str) or not _OID.fullmatch(commit):
            raise GitError("INVALID_COMMIT", "A full root commit ID is required")
        try:
            current = run_git(repo, ["rev-parse", "--verify", "refs/heads/main"]).strip()
            ancestry = run_git(repo, ["rev-list", "--parents", "-n", "1", commit]).split()
        except GitError:
            raise GitError("INVALID_COMMIT", "Local baseline commit is unavailable") from None
        if current != commit:
            raise GitError("INVALID_COMMIT", "Local main does not match the baseline commit")
        if ancestry != [commit]:
            raise GitError("INVALID_COMMIT", "Baseline must be a root commit")

    @staticmethod
    def _imported_head(
        repo: Path, registry: SourceRegistry, project_id: str,
        expected_head: str, expected_source_branch: str,
    ) -> None:
        """Prove this is the registered full-history LOCAL import and current HEAD."""
        if not isinstance(expected_head, str) or not _OID.fullmatch(expected_head):
            raise GitError("INVALID_COMMIT", "A full imported commit ID is required")
        branch = validate_branch(expected_source_branch)
        project = registry.get_project(project_id)
        if (project is None or project["origin_kind"] != "local"
                or Path(project["repo_path"]) != repo
                or repo != registry.managed_root / project_id / "repo.git"):
            raise GitError("GITHUB_IMPORT_SOURCE_INVALID",
                           "Baseline source is not a registered managed LOCAL import")
        try:
            actual_head = run_git(repo, ["rev-parse", "--verify", "HEAD^{commit}"]).strip()
            actual_branch = run_git(repo, ["symbolic-ref", "--quiet", "HEAD"]).strip()
            branch_head = run_git(
                repo, ["rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}"],
            ).strip()
            shallow = run_git(repo, ["rev-parse", "--is-shallow-repository"]).strip()
            replacements = run_git(
                repo, ["for-each-ref", "--format=%(refname)", "refs/replace"],
            ).strip()
            grafts_location = run_git(repo, ["rev-parse", "--git-path", "info/grafts"]).strip()
        except GitError:
            raise GitError("GITHUB_IMPORT_HEAD_CHANGED",
                           "Imported committed HEAD could not be verified") from None
        grafts = Path(grafts_location)
        if not grafts.is_absolute():
            grafts = repo / grafts
        if shallow != "false" or replacements or grafts.exists() or grafts.is_symlink():
            raise GitError("GITHUB_IMPORT_HISTORY_UNSAFE",
                           "Imported commit history is incomplete or rewritten")
        if (actual_head != expected_head or branch_head != expected_head
                or actual_branch != f"refs/heads/{branch}"):
            raise GitError("GITHUB_IMPORT_HEAD_CHANGED",
                           "Imported committed HEAD changed before baseline push")
        _check_tree_features(repo, expected_head)

    def reconcile_imported_main(
        self, repo: Path, expected_import_head: str, *,
        registry: SourceRegistry, project_id: str, expected_source_branch: str,
        inspector: RepositoryInspector, provision_marker: str,
    ) -> BaselineResult:
        """Read only: confirm one imported HEAD was installed as remote main."""
        checked = self._prepared_repo(repo)
        self._confirmed_private(inspector, provision_marker)
        self._imported_head(checked, registry, project_id,
                            expected_import_head, expected_source_branch)
        refs = self._visible_refs(checked)
        if refs == {"refs/heads/main": expected_import_head}:
            return BaselineResult(self.repository.id, expected_import_head, "reconciled")
        if refs:
            raise GitError("GITHUB_IMPORT_BASELINE_CONFLICT",
                           "Remote repository has divergent or additional refs")
        raise GitError("GITHUB_IMPORT_PUSH_UNKNOWN",
                       "Imported baseline push is not confirmed; do not replay it")

    def bootstrap_imported_main(
        self, repo: Path, expected_import_head: str, *,
        registry: SourceRegistry, project_id: str, expected_source_branch: str,
        inspector: RepositoryInspector, provision_marker: str,
    ) -> BaselineResult:
        """Normal one-shot push of a LOCAL import's full HEAD history to empty main.

        The caller journals exact repository, commit, and ref intent before this
        write. A lost push response triggers read-only reconciliation only; this
        method never retries the push or creates/deletes another remote resource.
        """
        checked = self._prepared_repo(repo)
        self._confirmed_private(inspector, provision_marker)
        self._imported_head(checked, registry, project_id,
                            expected_import_head, expected_source_branch)
        refs = self._visible_refs(checked)
        if refs == {"refs/heads/main": expected_import_head}:
            return BaselineResult(self.repository.id, expected_import_head, "attached")
        if refs:
            raise GitError("GITHUB_IMPORT_BASELINE_CONFLICT",
                           "Remote repository is not empty or imported baseline differs")
        self._imported_head(checked, registry, project_id,
                            expected_import_head, expected_source_branch)
        # Re-read both identities immediately before the exceptional main write.
        # A remote ref created during local validation is a definite no-write conflict.
        self._confirmed_private(inspector, provision_marker)
        if self._visible_refs(checked):
            raise GitError("GITHUB_IMPORT_BASELINE_CONFLICT",
                           "Remote repository changed before imported baseline push")
        write_uncertain = False
        try:
            _run_network_git(
                checked,
                ["push", "--porcelain", "--no-verify", "--no-follow-tags",
                 "--recurse-submodules=no", self.url,
                 f"{expected_import_head}:refs/heads/main"],
                self._token(), timeout=120,
            )
        except Exception:  # noqa: BLE001 - accepted push may have lost its response
            write_uncertain = True
        try:
            observed = self._visible_refs(checked)
        except GitError:
            raise GitError("GITHUB_IMPORT_PUSH_UNKNOWN",
                           "Imported baseline outcome requires read-only reconciliation") from None
        if observed == {"refs/heads/main": expected_import_head}:
            return BaselineResult(self.repository.id, expected_import_head,
                                  "reconciled" if write_uncertain else "created")
        if observed:
            raise GitError("GITHUB_IMPORT_BASELINE_CONFLICT",
                           "Remote repository changed during imported baseline push")
        raise GitError("GITHUB_IMPORT_PUSH_UNKNOWN",
                       "Imported baseline outcome requires read-only reconciliation")

    def _visible_refs(self, repo: Path) -> dict[str, str]:
        raw = _run_network_git(repo, ["ls-remote", "--refs", self.url],
                               self._token(), timeout=30)
        refs: dict[str, str] = {}
        for line in raw.splitlines():
            try:
                oid, ref = line.decode("ascii").split("	", 1)
            except (UnicodeError, ValueError):
                raise GitError("GITHUB_GIT_RESPONSE_INVALID",
                               "Remote ref inventory is malformed") from None
            if (not _OID.fullmatch(oid) or not ref.startswith("refs/")
                    or any(ch in ref for ch in "\0\r\n\t ") or ref in refs):
                raise GitError("GITHUB_GIT_RESPONSE_INVALID",
                               "Remote ref inventory is malformed")
            refs[ref] = oid
        return refs

    def reconcile_initial_main(self, repo: Path, expected_root_commit: str, *,
                               inspector: RepositoryInspector,
                               provision_marker: str) -> BaselineResult:
        """Read only: prove a previous baseline push, never issue another push."""
        checked = self._prepared_repo(repo)
        self._confirmed_private(inspector, provision_marker)
        self._root_commit(checked, expected_root_commit)
        refs = self._visible_refs(checked)
        if refs == {"refs/heads/main": expected_root_commit}:
            return BaselineResult(self.repository.id, expected_root_commit, "reconciled")
        if refs:
            raise GitError("GITHUB_BASELINE_CONFLICT",
                           "Remote repository has divergent or additional refs")
        raise GitError("GITHUB_BASELINE_PUSH_UNKNOWN",
                       "Baseline push is not yet confirmed; do not replay it")

    def bootstrap_main(self, repo: Path, expected_root_commit: str, *,
                       inspector: RepositoryInspector,
                               provision_marker: str) -> BaselineResult:
        """One normal push of an exact local root commit to a verified empty repo.

        This method is only for the owner-approved initial provisioning step. An
        ambiguous push is reconciled by reading refs and never retried here.
        """
        checked = self._prepared_repo(repo)
        self._confirmed_private(inspector, provision_marker)
        self._root_commit(checked, expected_root_commit)
        refs = self._visible_refs(checked)
        if refs == {"refs/heads/main": expected_root_commit}:
            return BaselineResult(self.repository.id, expected_root_commit, "attached")
        if refs:
            raise GitError("GITHUB_BASELINE_CONFLICT",
                           "Remote repository is not empty or baseline differs")
        write_uncertain = False
        try:
            _run_network_git(
                checked,
                ["push", "--porcelain", "--no-verify", "--no-follow-tags",
                 "--recurse-submodules=no", self.url,
                 f"{expected_root_commit}:refs/heads/main"],
                self._token(), timeout=120,
            )
        except Exception:  # noqa: BLE001 - a sent push may have lost its response
            write_uncertain = True
        try:
            observed = self._visible_refs(checked)
        except GitError:
            raise GitError("GITHUB_BASELINE_PUSH_UNKNOWN",
                           "Baseline push outcome requires read-only reconciliation") from None
        if observed == {"refs/heads/main": expected_root_commit}:
            return BaselineResult(self.repository.id, expected_root_commit,
                                  "reconciled" if write_uncertain else "created")
        if observed:
            raise GitError("GITHUB_BASELINE_CONFLICT",
                           "Remote repository changed during baseline push")
        raise GitError("GITHUB_BASELINE_PUSH_UNKNOWN",
                       "Baseline push outcome requires read-only reconciliation")
