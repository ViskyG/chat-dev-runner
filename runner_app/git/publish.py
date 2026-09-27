"""Publish one attested source snapshot to an approved local Git receiver.

This is the offline Git transport used by EP-05 acceptance. Cloud credentials and
GitHub identity are separate provider concerns; callers journal a push intent and
hold the repository lock before entering this finite step.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from runner_app.adapters.github import GitHubRepository
from runner_app.git.argv import GitError, run_git, validate_branch
from runner_app.git.changes import ChangeError, safe_source_path
from runner_app.git.registry import SourceRegistry
from runner_app.git.snapshots import workspace_fingerprint
from runner_app.git.worktrees import (
    _inspect_config,
    _reject_object_alternates,
    _repo,
    _workspace,
)
from runner_app.storage.paths import open_private_file

_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_REMOTE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}\Z")


class PublicationError(RuntimeError):
    """Stable refusal with enough non-secret ref information for a partial result."""

    def __init__(self, code: str, message: str, *, remote_head: str | None = None) -> None:
        self.code = code
        self.remote_head = remote_head
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class GitPublication:
    status: str
    snapshot_id: str
    commit: str
    tree: str
    remote_ref: str
    remote_head: str | None


def _approved_untracked(snapshot: Mapping[str, Any]) -> tuple[str, ...]:
    """Read only the already checked immutable manifest's capture path list."""
    try:
        descriptor = open_private_file(Path(snapshot["manifest_path"]))
        with os.fdopen(descriptor, "rb") as stream:
            raw = stream.read(1_048_577)
        if len(raw) > 1_048_576:
            raise ValueError("Oversize manifest")
        if hashlib.sha256(raw).hexdigest() != snapshot["manifest_sha256"]:
            raise ValueError("Changed manifest")
        manifest = json.loads(raw)
        paths = manifest["approved_untracked_paths"]
        if (manifest["snapshot_id"] != snapshot["snapshot_id"]
                or not isinstance(paths, list)
                or any(not isinstance(path, str) or str(safe_source_path(path)) != path
                       for path in paths)):
            raise ValueError("Invalid manifest")
        return tuple(paths)
    except (OSError, ValueError, KeyError, TypeError, ChangeError) as error:
        raise PublicationError("SNAPSHOT_CHANGED", "Immutable snapshot manifest is unavailable") from error


def _remote_head(repo: Path, remote: str, ref: str) -> str | None:
    try:
        lines = run_git(repo, ["ls-remote", "--refs", "--heads", remote, ref]).splitlines()
    except GitError as error:
        raise PublicationError("REMOTE_UNAVAILABLE", "Approved Git receiver could not be read") from error
    if not lines:
        return None
    if len(lines) != 1:
        raise PublicationError("REMOTE_INVALID", "Git receiver returned ambiguous branch state")
    fields = lines[0].split("\t")
    if len(fields) != 2 or fields[1] != ref or _OID.fullmatch(fields[0]) is None:
        raise PublicationError("REMOTE_INVALID", "Git receiver returned invalid branch state")
    return fields[0]


def _receiver(repo: Path, remote_name: str, remote_url: Path,
              default_branch: str) -> str:
    if not isinstance(remote_name, str) or _REMOTE.fullmatch(remote_name) is None:
        raise PublicationError("REMOTE_NOT_ALLOWED", "Git receiver name is invalid")
    remote_url = Path(remote_url)
    if (not remote_url.is_absolute() or ".." in remote_url.parts
            or remote_url.is_symlink() or not remote_url.is_dir()
            or remote_url.resolve() != remote_url):
        raise PublicationError("REMOTE_NOT_ALLOWED", "Offline Git receiver path is invalid")
    _inspect_config(remote_url)
    _reject_object_alternates(remote_url)
    hooks = remote_url / "hooks"
    if hooks.is_symlink() or (hooks.exists() and (not hooks.is_dir() or any(
        item.is_symlink() or item.suffix != ".sample" for item in hooks.iterdir()
    ))):
        raise PublicationError("REMOTE_NOT_ALLOWED", "Offline Git receiver has active hooks")
    if run_git(remote_url, ["rev-parse", "--is-bare-repository"]).strip() != "true":
        raise PublicationError("REMOTE_NOT_ALLOWED", "Offline Git receiver must be bare")
    if (remote_url.stat().st_uid != os.geteuid()
            or remote_url.stat().st_mode & 0o022):
        raise PublicationError("REMOTE_NOT_ALLOWED", "Offline Git receiver is not owner controlled")
    _inspect_config(repo)
    configured = [item for item in run_git(
        repo, ["config", "--no-includes", "--local", "--null", "--get-all",
               f"remote.{remote_name}.url"]
    ).split("\x00") if item]
    if configured != [str(remote_url)]:
        raise PublicationError("REMOTE_NOT_ALLOWED", "Git receiver differs from approved configuration")
    validate_branch(default_branch)
    try:
        actual_default = run_git(remote_url, ["symbolic-ref", "--quiet", "HEAD"]).strip()
    except GitError as error:
        raise PublicationError("REMOTE_INVALID", "Git receiver has no identifiable default branch") from error
    if actual_default != f"refs/heads/{default_branch}":
        raise PublicationError("DEFAULT_BRANCH_CHANGED", "Git receiver default branch changed")
    return actual_default


@dataclass(frozen=True)
class _PublishSource:
    repo: Path
    worktree: Path
    snapshot: Mapping[str, Any]
    workspace: Mapping[str, Any]
    snapshot_id: str
    commit: str
    tree: str
    ref: str


def _checked_source(
    registry: SourceRegistry, *, run_id: str, project_id: str,
    workspace_id: str, branch: str, default_branch: str,
) -> _PublishSource:
    branch = validate_branch(branch)
    default_branch = validate_branch(default_branch)
    if branch == default_branch:
        raise PublicationError("PROTECTED_BRANCH", "Default branch cannot be published by this route")
    target = registry.publication_target(run_id, project_id=project_id,
                                         workspace_id=workspace_id)
    snapshot = registry.get_snapshot(target["snapshot_id"])
    if snapshot is None:
        raise PublicationError("SNAPSHOT_MISSING", "Attested source snapshot is unavailable")
    project = registry.get_project(project_id)
    workspace = registry.get_workspace(project_id, workspace_id)
    if project is None or workspace is None or workspace["archived"]:
        raise PublicationError("WORKSPACE_CHANGED", "Publication workspace is unavailable")
    repo = _repo(Path(project["repo_path"]))
    worktree = _workspace(repo, Path(workspace["worktree_path"]))
    if repo != registry.managed_root / project_id / "repo.git":
        raise PublicationError("WRONG_REPOSITORY", "Git repository differs from managed identity")
    if branch != workspace["branch"]:
        raise PublicationError("WORKSPACE_CHANGED", "Publication branch differs from workspace")
    snapshot_ref = f"refs/cdr/snapshots/{target['snapshot_id']}"
    commit, tree = target["commit"], target["tree"]
    if (run_git(repo, ["rev-parse", "--verify", f"{snapshot_ref}^{{commit}}"] ).strip() != commit
            or run_git(repo, ["rev-parse", "--verify", f"{commit}^{{tree}}"] ).strip() != tree):
        raise PublicationError("SNAPSHOT_CHANGED", "Retained source object differs from tested run")
    return _PublishSource(repo, worktree, snapshot, workspace,
                          target["snapshot_id"], commit, tree, f"refs/heads/{branch}")


def _check_editor(source: _PublishSource, branch: str) -> None:
    approved_untracked = _approved_untracked(source.snapshot)
    try:
        current_head = run_git(source.worktree, ["rev-parse", "HEAD"]).strip()
        current_branch = run_git(source.worktree,
                                 ["symbolic-ref", "--quiet", "--short", "HEAD"]).strip()
        current_fingerprint = workspace_fingerprint(
            source.worktree, approved_untracked=approved_untracked,
        )
    except (GitError, ChangeError, OSError) as error:
        raise PublicationError("WORKSPACE_CHANGED", "Workspace changed after tested snapshot") from error
    if (source.snapshot["base_head"] != source.workspace["head"]
            or source.snapshot["source_generation"] != source.workspace["generation"]
            or current_head != source.snapshot["base_head"]
            or current_branch != branch
            or current_fingerprint != source.snapshot["capture_fingerprint"]):
        raise PublicationError("WORKSPACE_CHANGED", "Workspace changed after tested snapshot")


def check_tested_snapshot_editor(
    registry: SourceRegistry, *, run_id: str, project_id: str,
    workspace_id: str, branch: str, default_branch: str,
) -> None:
    """Read-only pre-intent refusal; publishers repeat this after journaling."""
    source = _checked_source(
        registry, run_id=run_id, project_id=project_id,
        workspace_id=workspace_id, branch=branch, default_branch=default_branch,
    )
    _check_editor(source, branch)


def _check_remote_advance(source: _PublishSource, remote_head: str | None) -> None:
    if remote_head is None:
        return
    try:
        run_git(source.repo, ["merge-base", "--is-ancestor", remote_head, source.commit])
    except GitError as error:
        raise PublicationError("REMOTE_DIVERGED", "Tested commit cannot fast-forward receiver",
                               remote_head=remote_head) from error


def _head_matches_expected(remote_head: str | None, expected: str | None) -> None:
    if remote_head != expected:
        raise PublicationError("REMOTE_CHANGED", "Workspace branch changed at Git receiver",
                               remote_head=remote_head)


def publish_tested_snapshot(
    registry: SourceRegistry, *, run_id: str, project_id: str,
    workspace_id: str, branch: str, remote_name: str, remote_url: Path,
    default_branch: str, expected_remote_head: str | None,
) -> GitPublication:
    """Normal push of the exact contained-run commit to a local bare receiver.

    A read-only retry may report an already confirmed remote commit even after
    editor changes. A fresh push always checks the target editor immediately
    before the remote effect and never advances it after a mismatch.
    """
    if expected_remote_head is not None and (
        not isinstance(expected_remote_head, str) or _OID.fullmatch(expected_remote_head) is None
    ):
        raise PublicationError("INVALID_REMOTE_HEAD", "Expected remote head must be a full commit ID")
    source = _checked_source(
        registry, run_id=run_id, project_id=project_id,
        workspace_id=workspace_id, branch=branch, default_branch=default_branch,
    )
    _receiver(source.repo, remote_name, remote_url, default_branch)
    remote_head = _remote_head(source.repo, remote_name, source.ref)
    if remote_head == source.commit:
        return GitPublication("ALREADY_PUBLISHED", source.snapshot_id, source.commit,
                              source.tree, source.ref, source.commit)
    _head_matches_expected(remote_head, expected_remote_head)
    _check_editor(source, branch)
    _check_remote_advance(source, remote_head)
    if source.commit == source.snapshot["base_head"]:
        return GitPublication("NO_DIFF", source.snapshot_id, source.commit,
                              source.tree, source.ref, remote_head)
    try:
        run_git(source.repo, ["push", "--porcelain", "--no-verify", "--no-follow-tags",
                              remote_name, f"{source.commit}:{source.ref}"], timeout=120)
    except GitError as error:
        try:
            observed = _remote_head(source.repo, remote_name, source.ref)
        except PublicationError as read_error:
            raise PublicationError("PUSH_OUTCOME_UNKNOWN",
                                   "Git push failed and receiver state is unavailable") from read_error
        if observed == source.commit:
            return GitPublication("PUBLISHED_RECONCILED", source.snapshot_id,
                                  source.commit, source.tree, source.ref, source.commit)
        code = "REMOTE_CHANGED" if observed != remote_head else "PUSH_FAILED"
        raise PublicationError(code, "Git receiver did not confirm tested commit",
                               remote_head=observed) from error
    try:
        observed = _remote_head(source.repo, remote_name, source.ref)
    except PublicationError as read_error:
        raise PublicationError("PUSH_OUTCOME_UNKNOWN",
                               "Git receiver could not confirm tested commit") from read_error
    if observed != source.commit:
        raise PublicationError("PUSH_OUTCOME_UNKNOWN",
                               "Git receiver did not confirm tested commit", remote_head=observed)
    return GitPublication("PUBLISHED", source.snapshot_id, source.commit,
                          source.tree, source.ref, source.commit)


class GitHubPushPort(Protocol):
    """Trusted owner-process transport bound to one exact HTTPS GitHub identity."""

    repository: GitHubRepository
    url: str

    def read_head(self, repo: Path, branch: str) -> str | None: ...

    def push_commit(self, repo: Path, commit: str, branch: str) -> None: ...


def publish_tested_snapshot_github(
    registry: SourceRegistry, transport: GitHubPushPort, *,
    run_id: str, project_id: str, workspace_id: str, branch: str,
    default_branch: str, expected_remote_head: str | None,
) -> GitPublication:
    """Authenticated exact-commit GitHub push with read-only ambiguity handling.

    The caller verifies the numeric/private REST repository identity and journals
    the exact push intent before entering this step. No force, merge, deletion or
    mutable editor branch is used as the source.
    """
    if expected_remote_head is not None and (
        not isinstance(expected_remote_head, str) or _OID.fullmatch(expected_remote_head) is None
    ):
        raise PublicationError("INVALID_REMOTE_HEAD", "Expected remote head must be a full commit ID")
    source = _checked_source(
        registry, run_id=run_id, project_id=project_id,
        workspace_id=workspace_id, branch=branch, default_branch=default_branch,
    )
    remote_head = transport.read_head(source.repo, branch)
    if remote_head == source.commit:
        return GitPublication("ALREADY_PUBLISHED", source.snapshot_id, source.commit,
                              source.tree, source.ref, source.commit)
    _head_matches_expected(remote_head, expected_remote_head)
    _check_editor(source, branch)
    _check_remote_advance(source, remote_head)
    if source.commit == source.snapshot["base_head"]:
        return GitPublication("NO_DIFF", source.snapshot_id, source.commit,
                              source.tree, source.ref, remote_head)
    # Narrow the race window after checking editor bytes. The transport itself
    # uses a normal non-force push; Git will reject any non-fast-forward race.
    observed_before = transport.read_head(source.repo, branch)
    _head_matches_expected(observed_before, remote_head)
    try:
        transport.push_commit(source.repo, source.commit, branch)
    except GitError as error:
        try:
            observed = transport.read_head(source.repo, branch)
        except GitError as read_error:
            raise PublicationError("PUSH_OUTCOME_UNKNOWN",
                                   "GitHub push outcome could not be read") from read_error
        if observed == source.commit:
            return GitPublication("PUBLISHED_RECONCILED", source.snapshot_id,
                                  source.commit, source.tree, source.ref, source.commit)
        code = "REMOTE_CHANGED" if observed != remote_head else "PUSH_FAILED"
        raise PublicationError(code, "GitHub did not confirm tested commit",
                               remote_head=observed) from error
    try:
        observed = transport.read_head(source.repo, branch)
    except GitError as read_error:
        raise PublicationError("PUSH_OUTCOME_UNKNOWN",
                               "GitHub could not confirm tested commit") from read_error
    if observed != source.commit:
        raise PublicationError("PUSH_OUTCOME_UNKNOWN",
                               "GitHub did not confirm tested commit", remote_head=observed)
    return GitPublication("PUBLISHED", source.snapshot_id, source.commit,
                          source.tree, source.ref, source.commit)


@dataclass(frozen=True)
class GitBootstrap:
    status: str
    commit: str
    remote_ref: str
    remote_head: str


def bootstrap_new_project_main(
    registry: SourceRegistry, *, project_id: str, remote_name: str,
    remote_url: Path, expected_initial_commit: str,
) -> GitBootstrap:
    """Install a new managed project's initial main commit into an empty receiver.

    This is the sole intentional default-branch push route. The caller must hold
    the repository lock and journal the exact resource/commit intent first.
    """
    if not isinstance(expected_initial_commit, str) or _OID.fullmatch(expected_initial_commit) is None:
        raise PublicationError("INVALID_COMMIT", "Initial source commit must be a full object ID")
    project = registry.get_project(project_id)
    if project is None or project["origin_kind"] != "new":
        raise PublicationError("WRONG_SOURCE_KIND", "Main bootstrap requires a newly provisioned source")
    repo = _repo(Path(project["repo_path"]))
    if repo != registry.managed_root / project_id / "repo.git":
        raise PublicationError("WRONG_REPOSITORY", "Git repository differs from managed identity")
    try:
        lineage = run_git(repo, ["rev-list", "--parents", "-n", "1",
                                 expected_initial_commit]).strip().split()
        run_git(repo, ["merge-base", "--is-ancestor", expected_initial_commit,
                       "refs/heads/main"])
    except GitError as error:
        raise PublicationError("SOURCE_CHANGED", "Initial source commit is unavailable") from error
    if lineage != [expected_initial_commit]:
        raise PublicationError("SOURCE_CHANGED", "Requested source commit is not an initial commit")
    _receiver(repo, remote_name, remote_url, "main")
    ref = "refs/heads/main"
    remote_head = _remote_head(repo, remote_name, ref)
    if remote_head == expected_initial_commit:
        return GitBootstrap("ALREADY_BOOTSTRAPPED", expected_initial_commit,
                            ref, expected_initial_commit)
    if remote_head is not None:
        raise PublicationError("REMOTE_CHANGED", "Receiver main already contains another commit",
                               remote_head=remote_head)
    if run_git(Path(remote_url), ["for-each-ref", "--count=1",
                                  "--format=%(refname)", "refs"]).strip():
        raise PublicationError("REMOTE_NOT_EMPTY", "Receiver already contains Git refs")
    inventory = dict(line.split(": ", 1) for line in run_git(
        Path(remote_url), ["count-objects", "-v"]
    ).splitlines() if ": " in line)
    try:
        nonempty_objects = int(inventory["count"]) or int(inventory["in-pack"])
    except (KeyError, ValueError) as error:
        raise PublicationError("REMOTE_INVALID", "Receiver object inventory is invalid") from error
    if nonempty_objects:
        raise PublicationError("REMOTE_NOT_EMPTY", "Receiver has retained Git objects")
    try:
        source_head = run_git(repo, ["rev-parse", "--verify", "refs/heads/main^{commit}"]).strip()
        source_default = run_git(repo, ["symbolic-ref", "--quiet", "HEAD"]).strip()
    except GitError as error:
        raise PublicationError("SOURCE_CHANGED", "Initial source commit is unavailable") from error
    if source_default != ref or source_head != expected_initial_commit:
        raise PublicationError("SOURCE_CHANGED", "Managed main is not the expected initial commit")
    try:
        run_git(repo, ["push", "--porcelain", "--no-verify", "--no-follow-tags",
                       remote_name, f"{expected_initial_commit}:{ref}"], timeout=120)
    except GitError as error:
        try:
            observed = _remote_head(repo, remote_name, ref)
        except PublicationError as read_error:
            raise PublicationError("PUSH_OUTCOME_UNKNOWN",
                                   "Main bootstrap push failed and receiver is unavailable") from read_error
        if observed == expected_initial_commit:
            return GitBootstrap("BOOTSTRAPPED_RECONCILED", expected_initial_commit,
                                ref, expected_initial_commit)
        code = "REMOTE_CHANGED" if observed is not None else "PUSH_FAILED"
        raise PublicationError(code, "Receiver did not confirm initial commit",
                               remote_head=observed) from error
    observed = _remote_head(repo, remote_name, ref)
    if observed != expected_initial_commit:
        raise PublicationError("PUSH_OUTCOME_UNKNOWN",
                               "Receiver did not confirm initial commit", remote_head=observed)
    return GitBootstrap("BOOTSTRAPPED", expected_initial_commit, ref, expected_initial_commit)
