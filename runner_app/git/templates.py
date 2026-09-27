"""Small checked-in source starters selected only by an owner-approved template ID.

These bytes are data, not executed on the host. Environment installation remains a
separate approved preparation phase; a template does not grant runtime authority.
"""
from __future__ import annotations

import re

from runner_app.git.registry import SourceError


def starter_files(template_id: str, project_id: str) -> dict[str, bytes]:
    """Return fixed, reviewable source files for the two currently supported starters."""
    if not isinstance(project_id, str) or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", project_id) is None:
        raise SourceError("INVALID_PROJECT_ID", "A canonical project ID is required")
    if template_id == "files-only":
        return {
            "README.md": (
                f"# {project_id}\n\n"
                "Add project files in a workspace branch. This template has no test profile.\n"
            ).encode(),
        }
    if template_id == "python-basic":
        return {
            ".gitignore": b"__pycache__/\n.pytest_cache/\n*.pyc\n.venv/\n",
            "README.md": (
                f"# {project_id}\n\n"
                "A small Python starting point. Develop on a workspace branch and "
                "run the locally approved test profile.\n"
            ).encode(),
            "pyproject.toml": (
                "[project]\n"
                f'name = "{project_id}"\n'
                'version = "0.1.0"\n'
                'requires-python = ">=3.10"\n\n'
                "[tool.pytest.ini_options]\n"
                'testpaths = ["tests"]\n'
            ).encode(),
            "src/__init__.py": b"",
            "src/app.py": (
                b"def greet(name: str) -> str:\n"
                b'    return f"Hello, {name}!"\n'
            ),
            "tests/test_app.py": (
                b"from src.app import greet\n\n\n"
                b"def test_greet() -> None:\n"
                b'    assert greet("world") == "Hello, world!"\n'
            ),
        }
    raise SourceError("TEMPLATE_UNAVAILABLE", "Approved source starter is not installed")
