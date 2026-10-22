"""Read the same canonical schema files in editable checkouts and installed wheels."""
from __future__ import annotations

import json
from importlib.resources import files
from typing import Any

SCHEMA_NAMES = frozenset({
    "application-log", "changeset", "command", "operation-manifest", "output", "receipt",
    "run-manifest",
    "v2-mailbox", "v2-project", "v2-environment", "v2-approval", "v2-result", "handoff-manifest",
})


def load_schema(name: str) -> dict[str, Any]:
    if name not in SCHEMA_NAMES:
        raise ValueError("Unknown schema name")
    value: dict[str, Any] = json.loads(files("schemas").joinpath(f"{name}.schema.json").read_text())
    return value
