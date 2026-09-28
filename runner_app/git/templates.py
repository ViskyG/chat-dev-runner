"""Small checked-in source starters selected only by an owner-approved template ID.

These bytes are data, not executed on the host. Environment installation remains a
separate approved preparation phase; a template does not grant runtime authority.
"""
from __future__ import annotations

import hashlib
import re
from importlib.resources import files

from runner_app.git.registry import SourceError

# The starter carries its own audited, pure-Python wheelhouse. Preparation uses
# --no-index and --require-hashes, then the test phase imports only staged /build.
_PYTHON_QUICK_WHEELS = {
    "iniconfig-2.3.0-py3-none-any.whl": "f631c04d2c48c52b84d0d0549c99ff3859c98df65b3101406327ecc7d53fbf12",
    "packaging-26.3-py3-none-any.whl": "d7193f7c8e4e93f444fde0262bf90af30e16fa0ad0ad44cb553c87339b23cd1c",
    "pluggy-1.6.0-py3-none-any.whl": "e920276dd6813095e9377c0bc5566d94c932c33b27a3e3945d8389c374dd4746",
    "pygments-2.21.0-py3-none-any.whl": "2363c69b61c4a97c838da3b130dcd6468f4848992b21a82f2a63ec34377137d9",
    "pytest-9.1.1-py3-none-any.whl": "37a86b45efb9a47a61a36449063e8e18d0cab3161329fc099eb21783169c4f0c",
}


def _python_quick_assets() -> dict[str, bytes]:
    assets: dict[str, bytes] = {}
    requirements: list[str] = []
    wheel_dir = files("runner_app.git").joinpath("python_quick_wheels")
    for name, expected in sorted(_PYTHON_QUICK_WHEELS.items()):
        try:
            wheel = wheel_dir.joinpath(name).read_bytes()
        except OSError:
            raise SourceError("TEMPLATE_UNAVAILABLE", "Python starter wheelhouse is unavailable") from None
        if hashlib.sha256(wheel).hexdigest() != expected:
            raise SourceError("TEMPLATE_UNAVAILABLE", "Python starter wheelhouse changed")
        assets[f"vendor/wheels/{name}"] = wheel
        package, version, *_ = name.split("-")
        requirements.append(f"{package}=={version} --hash=sha256:{expected}\n")
    assets["requirements.lock"] = ("# Offline Python 3.12 test dependencies; all wheels are bundled.\n"
                                    + "".join(requirements)).encode("ascii")
    return assets


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
            **_python_quick_assets(),
            ".gitignore": b"__pycache__/\n.pytest_cache/\n*.pyc\n.venv/\n",
            "README.md": (
                f"# {project_id}\n\n"
                "A small Python starting point. Develop on a workspace branch and "
                "run the locally approved test profile. The checked-in wheelhouse "
                "and requirements.lock prepare pytest without network access.\n"
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
