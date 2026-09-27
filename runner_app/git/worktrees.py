"""Runner-owned local repositories and retained worktrees.

The caller must hold the project's durable repository lock and resolve LOCAL imports
from an owner-issued opaque handle. This module never discovers arbitrary host paths
from a remote command, executes project code, or accesses a network remote.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from runner_app.git.argv import GitError, run_git, run_git_bytes, validate_branch
from runner_app.storage.paths import UnsafePathError, ensure_private_directory

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$", re.ASCII)
_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$", re.ASCII)
_REMOTE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$", re.ASCII)
_BLOCKED_CONFIG_PREFIXES = (
    "alias.", "credential.", "diff.", "filter.", "gpg.", "include.", "includeif.",
    "lfs.", "merge.", "protocol.", "submodule.", "uploadpack.", "url.",
)
_BLOCKED_CONFIG_KEYS = frozenset({
    "core.alternaterefscommand", "core.attributesfile", "core.editor", "core.fsmonitor",
    "core.hookspath", "core.sshcommand", "core.sparsecheckout",
    "core.sparsecheckoutcone", "core.worktree", "interactive.difffilter",
})


def _id(value: str, kind: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise GitError("INVALID_ID", f"{kind} must be a short lower-case identifier")
    return value


def _sha(value: str) -> str:
    if not isinstance(value, str) or not _SHA_RE.fullmatch(value):
        raise GitError("INVALID_COMMIT", "Expected a full Git commit object ID")
    return value


def _remote(value: str) -> str:
    if not isinstance(value, str) or not _REMOTE_RE.fullmatch(value):
        raise GitError("INVALID_REMOTE", "Remote name is not approved")
    return value


def _private(path: Path) -> None:
    try:
        ensure_private_directory(path)
    except (OSError, UnsafePathError) as error:
        raise GitError("UNSAFE_MANAGED_ROOT", "Managed source directory is not private") from error


def _project(managed_root: Path, project_id: str) -> Path:
    root = Path(managed_root)
    if not root.is_absolute() or ".." in root.parts:
        raise GitError("UNSAFE_MANAGED_ROOT", "Managed root must be an absolute local path")
    _private(root)
    project = root / _id(project_id, "Project ID")
    _private(project)
    return project


def _repo(repo: Path) -> Path:
    path = Path(repo)
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise GitError("INVALID_REPOSITORY", "Repository must be an existing managed directory")
    if path.name != "repo.git" or path.parent.is_symlink():
        raise GitError("INVALID_REPOSITORY", "Repository path does not use the managed layout")
    _id(path.parent.name, "Project ID")
    _private(path)
    return path


def _workspace(repo: Path, worktree: Path) -> Path:
    repo = _repo(repo)
    path = Path(worktree)
    expected_parent = repo.parent / "workspaces"
    if expected_parent.is_symlink():
        raise GitError("INVALID_WORKSPACE", "Workspace parent cannot be a symlink")
    if (not path.is_absolute() or path.is_symlink() or path.parent != expected_parent
            or not path.is_dir()):
        raise GitError("INVALID_WORKSPACE", "Workspace path does not use the managed layout")
    _id(path.name, "Workspace ID")
    _private(expected_parent)
    common = run_git(path, ["rev-parse", "--git-common-dir"]).strip()
    common_path = Path(common) if os.path.isabs(common) else path / common
    if common_path.resolve() != repo.resolve():
        raise GitError("INVALID_WORKSPACE", "Workspace is not linked to the selected repository")
    return path


def _inspect_config(cwd: Path, *, pass_fds: tuple[int, ...] = ()) -> None:
    raw = run_git(cwd, ["config", "--no-includes", "--local", "--null", "--list"],
                  pass_fds=pass_fds)
    for entry in raw.split("\x00"):
        if not entry:
            continue
        key, separator, _ = entry.partition("\n")
        if not separator:
            raise GitError("UNSAFE_GIT_CONFIG", "Repository configuration is malformed")
        key = key.lower()
        if (key in _BLOCKED_CONFIG_KEYS or key.startswith(_BLOCKED_CONFIG_PREFIXES)
                or (key.startswith("extensions.") and key != "extensions.objectformat")):
            raise GitError("UNSAFE_GIT_CONFIG", "Repository requires an unsupported Git feature")
        if key.startswith("remote.") and key.endswith((
            ".uploadpack", ".receivepack", ".vcs", ".proxy", ".promisor",
            ".partialclonefilter", ".pushurl",
        )):
            raise GitError("UNSAFE_GIT_CONFIG", "Repository remote has an unsafe transport setting")


def _reject_object_alternates(cwd: Path, *, pass_fds: tuple[int, ...] = ()) -> None:
    location = run_git(cwd, ["rev-parse", "--git-path", "objects/info/alternates"],
                       pass_fds=pass_fds).strip()
    candidate = Path(location) if os.path.isabs(location) else cwd / location
    if candidate.exists() or candidate.is_symlink():
        raise GitError("OBJECT_ALTERNATES_UNSUPPORTED",
                       "Repository refers to an external Git object store")


def _commit(repo: Path, commit: str) -> str:
    _sha(commit)
    resolved = run_git(repo, ["rev-parse", "--verify", f"{commit}^{{commit}}"] ).strip()
    if resolved != commit:
        raise GitError("INVALID_COMMIT", "Commit does not resolve to the requested object")
    return commit


def _check_tree_features(repo: Path, commit: str) -> None:
    """Refuse features that could invoke filters or require hidden source material."""
    tree = run_git_bytes(repo, ["ls-tree", "-r", "-z", "--full-tree", commit])
    for entry in tree.split(b"\x00"):
        if not entry:
            continue
        metadata, separator, path = entry.partition(b"\t")
        if not separator:
            raise GitError("UNSUPPORTED_SOURCE", "Git tree entry is malformed")
        mode = metadata.split(b" ", 1)[0]
        if mode == b"160000" or path == b".gitmodules" or path.endswith(b"/.gitmodules"):
            raise GitError("SUBMODULE_UNSUPPORTED", "Submodules require a separate approved profile")
        if path == b".lfsconfig" or path.endswith(b"/.lfsconfig"):
            raise GitError("LFS_UNSUPPORTED", "Git LFS configuration requires a separate profile")
        if path == b".gitattributes" or path.endswith(b"/.gitattributes"):
            attributes = run_git_bytes(repo, ["show", f"{commit}:{path.decode('utf-8', 'surrogateescape')}"])
            if b"filter=" in attributes.lower() or b"working-tree-encoding=" in attributes.lower():
                raise GitError("FILTER_UNSUPPORTED", "Checkout filters require a separate approved profile")


def _branch_occupant(repo: Path, branch: str) -> Path | None:
    # Git's %(worktreepath) atom is unambiguous even when the managed root has newlines.
    output = run_git(repo, ["for-each-ref", "--format=%(worktreepath)%00",
                            f"refs/heads/{branch}"])
    if not output:
        return None
    location = output.split("\x00", 1)[0]
    if not location or Path(location).resolve() == repo.resolve():
        return None  # A bare repository has no checkout occupying its HEAD branch.
    return Path(location)


def _head_and_branch(worktree: Path) -> tuple[str, str | None]:
    head = run_git(worktree, ["rev-parse", "HEAD"]).strip()
    try:
        branch = run_git(worktree, ["symbolic-ref", "--quiet", "--short", "HEAD"]).strip()
    except GitError:
        branch = None
    return head, branch


def _require_expected(worktree: Path, expected_head: str, expected_branch: str) -> None:
    _sha(expected_head)
    validate_branch(expected_branch)
    head, branch = _head_and_branch(worktree)
    if head != expected_head or branch != expected_branch:
        raise GitError("WORKSPACE_CHANGED", "Workspace HEAD or branch changed since admission")


def _require_clean(worktree: Path) -> None:
    if run_git(worktree, ["status", "--porcelain=v1", "-z", "--untracked-files=all",
                          "--ignored=matching"]):
        raise GitError("DIRTY_WORKSPACE", "Workspace has tracked or untracked edits")


def create_repository(managed_root: Path, project_id: str) -> Path:
    """Create a private bare repository with a reproducible empty main commit.

    The staging directory remains after a fault for operator inspection; only a complete
    bare repository is promoted to the stable project path.
    """
    project = _project(managed_root, project_id)
    repository = project / "repo.git"
    if repository.exists() or repository.is_symlink():
        raise GitError("PROJECT_EXISTS", "Managed repository already exists")
    stage = Path(tempfile.mkdtemp(prefix=".provision-", dir=project))
    seed = stage / "seed"
    seed.mkdir(mode=0o700)
    run_git(seed, ["init", "-b", "main"])
    run_git(seed, ["-c", "user.name=Chat Dev Runner", "-c", "user.email=runner@localhost.invalid",
                   "commit", "--allow-empty", "-m", "Initialize managed project"])
    bare = stage / "repo.git"
    run_git(stage, ["clone", "--bare", "--no-local", str(seed), str(bare)])
    _inspect_config(bare)
    if repository.exists() or repository.is_symlink():
        raise GitError("PROJECT_EXISTS", "Managed repository already exists")
    os.rename(bare, repository)
    shutil.rmtree(stage)
    return repository


def import_local_granted(managed_root: Path, project_id: str, source: str | Path,
                         *, pass_fds: tuple[int, ...] = ()) -> Path:
    """Import committed refs from an owner-granted local checkout without editing it.

    The caller, not a Drive request, resolves an opaque import handle to ``source``. Dirty
    files remain solely in the original; this clone captures its committed object graph.
    """
    source = Path(source)
    pinned = re.fullmatch(r"/proc/self/fd/([0-9]+)", str(source))
    if pinned:
        if int(pinned.group(1)) not in pass_fds:
            raise GitError("INVALID_IMPORT_SOURCE", "Pinned import descriptor was not provided")
    elif not source.is_absolute() or source.is_symlink() or not source.is_dir():
        raise GitError("INVALID_IMPORT_SOURCE", "Local import requires an owner-granted directory")
    _inspect_config(source, pass_fds=pass_fds)
    _reject_object_alternates(source, pass_fds=pass_fds)
    source_head = run_git(source, ["rev-parse", "--verify", "HEAD^{commit}"],
                          pass_fds=pass_fds).strip()
    _sha(source_head)
    project = _project(managed_root, project_id)
    repository = project / "repo.git"
    if repository.exists() or repository.is_symlink():
        raise GitError("PROJECT_EXISTS", "Managed repository already exists")
    stage = Path(tempfile.mkdtemp(prefix=".import-", dir=project))
    bare = stage / "repo.git"
    run_git(stage, ["clone", "--bare", "--local", "--no-hardlinks", str(source), str(bare)],
            pass_fds=pass_fds)
    _inspect_config(bare)
    _reject_object_alternates(bare)
    if run_git(bare, ["rev-parse", "HEAD"]).strip() != source_head:
        raise GitError("SOURCE_CHANGED", "Source HEAD changed while importing committed refs")
    _check_tree_features(bare, source_head)
    # Import is a one-time copy, not a writable link back to the human's checkout.
    if "origin" in run_git(bare, ["remote"]).splitlines():
        run_git(bare, ["remote", "remove", "origin"])
    if repository.exists() or repository.is_symlink():
        raise GitError("PROJECT_EXISTS", "Managed repository already exists")
    os.rename(bare, repository)
    shutil.rmtree(stage)
    return repository


def create_workspace(managed_root: Path, project_id: str, repo: Path, workspace_id: str,
                     branch: str, base_commit: str) -> Path:
    """Check out an exact commit on a distinct branch in a new managed worktree."""
    project = _project(managed_root, project_id)
    repo = _repo(repo)
    if repo != project / "repo.git":
        raise GitError("INVALID_REPOSITORY", "Repository does not belong to the selected project")
    _inspect_config(repo)
    commit = _commit(repo, base_commit)
    _check_tree_features(repo, commit)
    branch = validate_branch(branch)
    directory = project / "workspaces"
    _private(directory)
    workspace = directory / _id(workspace_id, "Workspace ID")
    if workspace.exists() or workspace.is_symlink():
        raise GitError("WORKSPACE_EXISTS", "Managed workspace already exists")
    occupant = _branch_occupant(repo, branch)
    if occupant is not None:
        raise GitError("BRANCH_IN_USE", "Branch is checked out in another worktree")
    try:
        branch_head = run_git(repo, ["rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}"] ).strip()
    except GitError:
        branch_head = None
    if branch_head is None:
        run_git(repo, ["worktree", "add", "-b", branch, str(workspace), commit])
    else:
        if branch_head != commit:
            raise GitError("STALE_BASE", "Existing branch differs from the requested base commit")
        run_git(repo, ["worktree", "add", str(workspace), branch])
    state = inspect_workspace(repo, workspace)
    if state["head"] != commit or state["branch"] != branch:
        raise GitError("WORKTREE_POSTCONDITION_FAILED", "Created worktree has unexpected source")
    return workspace


def inspect_workspace(repo: Path, worktree: Path, *, remote: str | None = None) -> dict[str, Any]:
    """Read source status, including fetch metadata without changing checkout files."""
    path = _workspace(repo, worktree)
    head, branch = _head_and_branch(path)
    dirty = bool(run_git(path, ["status", "--porcelain=v1", "-z", "--untracked-files=all",
                                    "--ignored=matching"]))
    upstream: str | None = None
    ahead = behind = 0
    if remote is not None:
        name = _remote(remote)
        if branch is not None:
            ref = f"refs/remotes/{name}/{branch}"
            try:
                run_git(path, ["rev-parse", "--verify", ref])
                upstream = f"{name}/{branch}"
            except GitError:
                pass
    else:
        try:
            upstream = run_git(path, ["rev-parse", "--abbrev-ref", "--symbolic-full-name",
                                      "@{upstream}"]).strip()
        except GitError:
            pass
    if upstream is not None:
        relation = run_git(path, ["rev-list", "--left-right", "--count", f"HEAD...{upstream}"])
        counts = relation.strip().split()
        if len(counts) != 2:
            raise GitError("GIT_METADATA_INVALID", "Git upstream relation is malformed")
        ahead, behind = int(counts[0]), int(counts[1])
    return {"head": head, "branch": branch, "dirty": dirty, "upstream": upstream,
            "ahead": ahead, "behind": behind, "diverged": ahead > 0 and behind > 0,
            "worktree": str(path)}


def retarget_workspace(repo: Path, worktree: Path, *, expected_head: str,
                       expected_branch: str, target_branch: str, target_commit: str,
                       busy: bool = False) -> dict[str, Any]:
    """Switch one clean, idle worktree to an unoccupied existing branch at its exact HEAD."""
    path = _workspace(repo, worktree)
    if busy:
        raise GitError("WORKSPACE_BUSY", "Workspace has an active operation")
    _inspect_config(path)
    _require_expected(path, expected_head, expected_branch)
    _require_clean(path)
    target_branch = validate_branch(target_branch)
    commit = _commit(repo, target_commit)
    _check_tree_features(repo, commit)
    branch_head = run_git(repo, ["rev-parse", "--verify", f"refs/heads/{target_branch}^{{commit}}"] ).strip()
    if branch_head != commit:
        raise GitError("STALE_TARGET", "Target branch no longer points to the requested commit")
    occupant = _branch_occupant(repo, target_branch)
    if occupant is not None and occupant.resolve() != path.resolve():
        raise GitError("BRANCH_IN_USE", "Target branch is checked out in another worktree")
    if target_branch != expected_branch:
        _require_expected(path, expected_head, expected_branch)
        _require_clean(path)
        run_git(path, ["switch", "--no-guess", "--no-overwrite-ignore", target_branch])
    state = inspect_workspace(repo, path)
    if state["head"] != commit or state["branch"] != target_branch:
        raise GitError("WORKSPACE_CHANGED", "Workspace changed during branch retarget")
    return state


def fetch(repo: Path, remote: str, *, allowed_urls: Sequence[str]) -> str:
    """Fetch only local allowlisted bare repos into remote-tracking refs, never a worktree."""
    repo = _repo(repo)
    _inspect_config(repo)
    name = _remote(remote)
    if isinstance(allowed_urls, (str, bytes)) or not allowed_urls:
        raise GitError("REMOTE_NOT_ALLOWED", "Remote URL allowlist must be explicit")
    configured = [item for item in run_git(
        repo, ["config", "--null", "--get-all", f"remote.{name}.url"]).split("\x00") if item]
    if len(configured) != 1 or configured[0] not in allowed_urls:
        raise GitError("REMOTE_NOT_ALLOWED", "Remote URL is not allowlisted for this project")
    url = configured[0]
    if not os.path.isabs(url) or not Path(url).is_dir() or Path(url).is_symlink():
        raise GitError("REMOTE_NOT_ALLOWED", "Offline fetch requires an absolute local repository")
    if run_git(Path(url), ["rev-parse", "--is-bare-repository"]).strip() != "true":
        raise GitError("REMOTE_NOT_ALLOWED", "Offline fetch requires a bare local repository")
    run_git(repo, ["fetch", "--no-tags", "--no-prune", "--no-recurse-submodules",
                   "--no-write-fetch-head",
                   name, f"+refs/heads/*:refs/remotes/{name}/*"], timeout=120)
    return name


def sync_workspace(repo: Path, worktree: Path, *, expected_head: str,
                   expected_branch: str, remote: str, remote_branch: str,
                   expected_target_commit: str | None = None,
                   busy: bool = False) -> dict[str, Any]:
    """Fast-forward an idle clean branch to already-fetched metadata; never fetch implicitly."""
    path = _workspace(repo, worktree)
    if busy:
        raise GitError("WORKSPACE_BUSY", "Workspace has an active operation")
    _inspect_config(path)
    _require_expected(path, expected_head, expected_branch)
    _require_clean(path)
    name = _remote(remote)
    branch = validate_branch(remote_branch)
    target_ref = f"refs/remotes/{name}/{branch}"
    target = run_git(repo, ["rev-parse", "--verify", f"{target_ref}^{{commit}}"] ).strip()
    if expected_target_commit is not None and target != _sha(expected_target_commit):
        raise GitError("STALE_TARGET", "Fetched branch changed since the command was prepared")
    _check_tree_features(repo, target)
    if target != expected_head:
        try:
            run_git(repo, ["merge-base", "--is-ancestor", expected_head, target])
        except GitError as error:
            raise GitError("SYNC_DIVERGED", "Fetched branch cannot fast-forward the workspace") from error
        _require_expected(path, expected_head, expected_branch)
        _require_clean(path)
        run_git(path, ["merge", "--ff-only", "--no-edit", "--no-overwrite-ignore", target])
    state = inspect_workspace(repo, path, remote=name)
    if state["head"] != target or state["branch"] != expected_branch:
        raise GitError("WORKSPACE_CHANGED", "Workspace changed during fast-forward sync")
    return state


def archive_workspace(repo: Path, worktree: Path, *, expected_head: str,
                      expected_branch: str, busy: bool = False) -> dict[str, Any]:
    """Return a retained archive record; caller atomically marks workspace inactive in DB.

    Dirty data stays in place. This function never removes or prunes a worktree.
    """
    path = _workspace(repo, worktree)
    if busy:
        raise GitError("WORKSPACE_BUSY", "Workspace has an active operation")
    _require_expected(path, expected_head, expected_branch)
    state = inspect_workspace(repo, path)
    return {"retained_path": str(path), "head": state["head"],
            "branch": state["branch"], "dirty": state["dirty"], "inactive": True}
