"""Strict project task contracts, resolved and frozen at execution start by the caller."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, cast

from runner_app.domain.commands import DomainError
from runner_app.domain.contracts_v2 import (
    canonical_bytes,
    parse_contract_json,
    validate_relative_path,
    validate_schema,
)


@dataclass(frozen=True)
class FrozenProject:
    canonical_payload: bytes
    digest: str

    @property
    def project(self) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(self.canonical_payload))


def validate_project_shape(project: dict[str, Any]) -> None:
    validate_schema(project, "v2-project")
    for task in project["tasks"].values():
        step_ids: set[str] = set()
        for step in task["steps"]:
            if step["id"] in step_ids:
                raise DomainError("INVALID_TASK", "Task step IDs must be unique within a task.")
            step_ids.add(step["id"])
            validate_relative_path(step["cwd"], allow_root=True)
            if any("\x00" in arg for arg in step["argv"]):
                raise DomainError("INVALID_TASK", "Task arguments cannot contain NUL characters.")
        output_ids: set[str] = set()
        for output in task["outputs"]:
            if output["id"] in output_ids:
                raise DomainError("INVALID_TASK", "Output IDs must be unique within a task.")
            output_ids.add(output["id"])
            validate_relative_path(output["path"])
        gate = task["quality_gate"]
        if not set(gate["required_outputs"]).issubset(output_ids):
            raise DomainError("INVALID_TASK", "Quality gate references an undefined output.")
        if gate["test_required"] and not any(step["phase"] == "test" for step in task["steps"]):
            raise DomainError("INVALID_TASK", "A required test gate needs a test phase step.")


def parse_project(raw: bytes) -> FrozenProject:
    project = parse_contract_json(raw)
    validate_project_shape(project)
    payload = canonical_bytes(project)
    return FrozenProject(payload, hashlib.sha256(payload).hexdigest())
