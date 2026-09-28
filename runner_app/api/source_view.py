"""Read current managed Git state for one exact registered workspace."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from runner_app.git import worktrees
from runner_app.git.argv import GitError
from runner_app.git.registry import SourceError, SourceRegistry


class SourceViewError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def inspect_registered_workspace(
    registry: SourceRegistry, project_id: str, workspace_id: str,
) -> dict[str, Any]:
    """Report observed Git facts separately from durable registry expectations."""
    try:
        repo = registry.checked_project_path(project_id)
        worktree = registry.checked_workspace_path(project_id, workspace_id)
        recorded = registry.get_workspace(project_id, workspace_id)
        assert recorded is not None
        observed = worktrees.inspect_workspace(repo, worktree)
    except SourceError as error:
        if error.code in {"PROJECT_NOT_REGISTERED", "WORKSPACE_NOT_REGISTERED"}:
            raise SourceViewError("SOURCE_NOT_FOUND") from None
        if error.code == "WORKSPACE_UNAVAILABLE":
            raise SourceViewError("WORKSPACE_UNAVAILABLE") from None
        raise SourceViewError("SOURCE_UNSAFE") from None
    except GitError:
        raise SourceViewError("SOURCE_INSPECTION_FAILED") from None
    return {
        "project_id": project_id,
        "workspace_id": workspace_id,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "recorded_head": recorded["head"],
        "recorded_branch": recorded["branch"],
        "recorded_generation": recorded["generation"],
        "observed_head": observed["head"],
        "observed_branch": observed["branch"],
        "dirty": observed["dirty"],
        "upstream": observed["upstream"],
        "ahead": observed["ahead"],
        "behind": observed["behind"],
        "diverged": observed["diverged"],
        "matches_recorded": (
            observed["head"] == recorded["head"]
            and observed["branch"] == recorded["branch"]
        ),
    }
