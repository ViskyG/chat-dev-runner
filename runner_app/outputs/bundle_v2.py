"""Retain originals locally and select bounded copies for the Git result bundle.

Reuse the v1 descriptor-relative collector: neither an output declaration nor a
glob can follow a project-created link into the controller's files. Publication
limits and local capture limits are deliberately separate. No retained result is
automatically removed by this module.
"""
from __future__ import annotations

import hashlib
import os
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runner_app.git.results_v2 import DEFAULT_RESULT_LIMITS, ResultLimits
from runner_app.outputs.collection import OutputError, OutputSpec, collect_outputs
from runner_app.storage.paths import ensure_private_directory


@dataclass(frozen=True)
class FrozenOutputs:
    artifacts: tuple[dict[str, Any], ...]
    originals: Mapping[str, Mapping[str, Path]]
    available_outputs: frozenset[str]
    invalid_outputs: frozenset[str]
    findings: tuple[str, ...]


def collect_bundle_outputs(
    roots: Mapping[str, Path], declarations: Sequence[Mapping[str, Any]],
    result_dir: Path, *, publication_limits: ResultLimits = DEFAULT_RESULT_LIMITS,
    max_local_file_bytes: int = 256 * 1024 * 1024,
    max_local_total_bytes: int = 1024 * 1024 * 1024,
) -> FrozenOutputs:
    """Create ``originals/`` and ``bundle/artifacts/`` under a new result dir.

    ``artifacts`` describes the verified local state at sealing time. Successful
    delivery is proved separately by the controller's mutable delivery.json;
    immutable artifacts never predict that a provider write has succeeded.
    """
    if min(max_local_file_bytes, max_local_total_bytes) < 1:
        raise ValueError("Local capture limits must be positive")
    ensure_private_directory(result_dir.parent)
    result_dir.mkdir(mode=0o700)
    retained = result_dir / "originals"
    retained.mkdir(mode=0o700)
    bundle = result_dir / "bundle"
    bundle.mkdir(mode=0o700)
    published = bundle / "artifacts"
    published.mkdir(mode=0o700)
    artifacts: list[dict[str, Any]] = []
    originals: dict[str, dict[str, Path]] = {}
    available: set[str] = set()
    invalid: set[str] = set()
    findings: list[str] = []
    local_bytes = published_bytes = 0
    identifiers: set[str] = set()
    artifact_ids: set[str] = set()
    reserved_ids = {str(item["id"]) for item in declarations}
    for declaration in declarations:
        output_id = str(declaration["id"])
        if output_id in identifiers:
            raise ValueError("Duplicate output ID")
        identifiers.add(output_id)
        originals[output_id] = {}
        relative = str(declaration["path"])
        root = roots[str(declaration["source_root"])]
        if (root / relative).is_dir():
            relative += ("/**/TEST-*.xml" if declaration["format"]
                         in {"surefire-xml", "failsafe-xml"} else "/**/*")
        spec = OutputSpec(output_id, str(declaration["kind"]), relative,
                          str(declaration["format"]), bool(declaration["required"]))
        remaining = max_local_total_bytes - local_bytes
        try:
            if remaining <= 0:
                raise OutputError("LOCAL_CAPTURE_LIMIT", "Local output budget exhausted")
            collection = collect_outputs(
                roots[str(declaration["source_root"])], [spec], retained / output_id,
                max_files=128, max_file_bytes=max_local_file_bytes,
                max_total_bytes=remaining,
            )
        except (OSError, OutputError, KeyError) as error:
            invalid.add(output_id)
            findings.append(f"{output_id}:{getattr(error, 'code', 'OUTPUT_CAPTURE_FAILED')}")
            collection = None
        if collection is not None:
            findings.extend(collection.findings)
            if not collection.complete:
                invalid.add(output_id)
            for item in collection.outputs:
                captured_id = str(item["output_id"])
                path = collection.files[captured_id]
                local_bytes += int(item["size_bytes"])
                source_path = str(collection.provenance[captured_id]["source_relative_path"])
                originals[output_id][source_path] = path
                artifact_id = output_id if captured_id == output_id else (
                    f"{output_id}-{hashlib.sha256(source_path.encode()).hexdigest()[:12]}")
                base_id, suffix = artifact_id, 0
                while artifact_id in artifact_ids or (captured_id != output_id and artifact_id in reserved_ids):
                    suffix += 1
                    artifact_id = f"{base_id}-{suffix}"
                artifact_ids.add(artifact_id)
                complete = bool(item["complete"])
                reason: str | None = None
                remote_path: str | None = None
                if not complete:
                    invalid.add(output_id)
                    reason = "LOCAL_CAPTURE_INCOMPLETE"
                elif declaration["publish"] == "local_only":
                    reason = "LOCAL_ONLY_POLICY"
                elif (item["size_bytes"] > publication_limits.per_file_bytes
                      or published_bytes + item["size_bytes"]
                      > max(0, publication_limits.bundle_bytes - 1024 * 1024)):
                    # Leave space for manifests, phase facts and human summary.
                    reason = "POLICY_SIZE_LIMIT"
                else:
                    namespace = published / output_id
                    namespace.mkdir(mode=0o700, exist_ok=True)
                    destination = namespace / path.name
                    shutil.copyfile(path, destination, follow_symlinks=False)
                    os.chmod(destination, 0o600)
                    published_bytes += int(item["size_bytes"])
                    remote_path = f"artifacts/{output_id}/{destination.name}"
                artifacts.append({
                    "id": artifact_id, "kind": declaration["kind"],
                    "format": declaration["format"], "bytes": item["size_bytes"],
                    "sha256": item["sha256"], "availability": "LOCAL_ONLY",
                    "local_path": str(path), "remote_path": remote_path,
                    "reason": reason or "PUBLICATION_PENDING", "complete": complete,
                })
            if originals[output_id] and output_id not in invalid:
                available.add(output_id)
        if not originals[output_id]:
            artifacts.append({
                "id": output_id, "kind": declaration["kind"], "format": declaration["format"],
                "bytes": 0, "sha256": hashlib.sha256(b"").hexdigest(),
                "availability": "MISSING", "local_path": None, "remote_path": None,
                "reason": "OUTPUT_MISSING_OR_UNSAFE", "complete": False,
            })
            if declaration["required"]:
                invalid.add(output_id)
    return FrozenOutputs(tuple(artifacts), originals, frozenset(available),
                         frozenset(invalid), tuple(findings))
