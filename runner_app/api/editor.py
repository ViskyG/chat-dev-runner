"""Owner-clicked VS Code launch for a registered managed workspace."""
from __future__ import annotations

import shutil
import subprocess
from typing import Any

from runner_app.git.registry import SourceError, SourceRegistry


class EditorLaunchError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def launch_editor(registry: SourceRegistry, project_id: str, workspace_id: str) -> dict[str, Any]:
    """Resolve IDs locally; never accept a path or executable from a command document."""
    try:
        path = registry.checked_workspace_path(project_id, workspace_id)
    except SourceError as error:
        if error.code == "WORKSPACE_NOT_REGISTERED":
            raise EditorLaunchError("WORKSPACE_NOT_FOUND") from None
        if error.code == "WORKSPACE_UNAVAILABLE":
            raise EditorLaunchError("WORKSPACE_UNAVAILABLE") from None
        raise EditorLaunchError("WORKSPACE_PATH_UNSAFE") from None

    executable = shutil.which("code")
    if executable is None:
        raise EditorLaunchError("EDITOR_UNAVAILABLE")
    try:
        subprocess.Popen(
            [executable, "--reuse-window", str(path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    except OSError:
        raise EditorLaunchError("EDITOR_LAUNCH_FAILED") from None
    return {"status": "OPEN_REQUESTED", "project_id": project_id,
            "workspace_id": workspace_id}
