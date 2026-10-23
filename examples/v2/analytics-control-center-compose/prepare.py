"""Prepare an owner-local released Control Center UI project; never execute it."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9-]{0,47}\Z")
IMAGE = re.compile(r"[a-z0-9][a-z0-9.:/-]*@sha256:[0-9a-f]{64}\Z")
ARGV = ["node", "/src/run-ui-contract.cjs"]


def encoded(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode()


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate release manifest field")
        result[key] = value
    return result


def release_identity(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError("release manifest exceeds 64 KiB")
    manifest = json.loads(raw, object_pairs_hook=unique_object)
    if manifest.get("schemaVersion") != 1 or manifest.get("targetPlatform") != "linux/amd64":
        raise ValueError("this adapter requires a schemaVersion 1 linux/amd64 release")
    source = manifest["source"]
    if not re.fullmatch(r"[0-9a-f]{40}", source["revision"]):
        raise ValueError("release source revision must be a full Git SHA")
    selected = {}
    for role, target in (("runtime", "runtime"), ("uiContract", "ui-contract")):
        item = manifest["images"][role]
        reference = item["reference"]
        if (not IMAGE.fullmatch(reference) or item["target"] != target
                or reference.rsplit("@", 1)[1] != item["digest"]):
            raise ValueError("release image reference/digest/target mismatch")
        selected[role] = reference
    return {
        "schema_version": "cdr-analytics-ui-release/1",
        "release_manifest_sha256": digest(raw),
        "release_version": manifest["releaseVersion"],
        "source": {key: source[key] for key in ("repository", "revision", "tag")},
        "platform": manifest["targetPlatform"],
        "images": selected,
        "contract": {"argv": ["npm", "run", "test:ui"], "cwd": "/work",
                     "base_url": "http://control-center:8768"},
        "scope": "Released image UI contract against bundled example catalog and temporary state",
    }


def prepare(manifest: Path, project_dir: Path, profile_dir: Path,
            project_id: str, profile_id: str) -> dict[str, Any]:
    if not IDENTIFIER.fullmatch(project_id) or not IDENTIFIER.fullmatch(profile_id):
        raise ValueError("project/profile IDs must be 1–48 lowercase letters, digits or hyphens")
    project_dir, profile_dir = project_dir.absolute(), profile_dir.absolute()
    if any(path.exists() or path.is_symlink() for path in (project_dir, profile_dir)):
        raise ValueError("both destination directories must be new; nothing is overwritten")
    project_dir, profile_dir = project_dir.resolve(), profile_dir.resolve()
    if (project_dir == profile_dir or project_dir in profile_dir.parents
            or profile_dir in project_dir.parents):
        raise ValueError("private profiles must be outside the project tree")
    if not project_dir.parent.is_dir() or not profile_dir.parent.is_dir():
        raise ValueError("destination parent directories must already exist")
    release = release_identity(manifest)
    runtime, browser = (release["images"][role] for role in ("runtime", "uiContract"))
    compose = {"services": {
        "control-center": {
            "image": runtime,
            "command": ["python3", "/app/server.py", "--host", "0.0.0.0",
                        "--allow-container-bind", "--port", "8768",
                        "--data-dir", "/data/control-center",
                        "--catalog-dir", "/app/examples/catalog", "--workspace-root", "/app",
                        "--allowed-origin", "http://control-center:8768"],
            "healthcheck": {"test": ["CMD", "python3", "-c",
                ("import json,urllib.request; r=urllib.request.urlopen("
                "'http://127.0.0.1:8768/api/v1/health',timeout=2); "
                "assert r.status == 200; assert json.load(r)['status'] in {'healthy','warning'}")],
                "interval": "2s", "timeout": "2s", "retries": 15}},
        "ui-contract": {"image": browser, "command": ARGV}}}
    compose_raw = encoded(compose)
    outputs = [
        {"id": "junit", "kind": "test_result", "path": "ui-contract.junit.xml",
         "format": "junit-xml", "required": True, "upload_policy": "local_only"},
        {"id": "ui-result", "kind": "artifact", "path": "ui-contract.json",
         "format": "json", "required": True, "upload_policy": "local_only"},
        {"id": "ui-log", "kind": "artifact", "path": "ui-contract.log",
         "format": "text", "required": False, "upload_policy": "local_only"},
    ]
    profile = {
        "schema_version": "cdr-compose-profile/1", "profile_id": profile_id,
        "image": browser, "allowed_images": list(dict.fromkeys([browser, runtime])),
        "platform": release["platform"], "compose_file": "compose.json",
        "compose_sha256": digest(compose_raw), "test_service": "ui-contract",
        "ready_services": ["control-center"], "migration_services": [], "argv": ARGV,
        "timeout_seconds": 600, "readiness_seconds": 60, "migration_seconds": 30,
        "cleanup_seconds": 10, "volume_mb": 1024, "memory_mb": 2048,
        "cpu_limit": 2, "pids_limit": 256, "max_log_bytes": 4 * 1024 * 1024,
        "mount_policy": "frozen-source-and-run-quota-v1", "collect": outputs,
    }
    project = {"protocol": "cdr-project/2", "project_id": project_id, "tasks": {"test": {
        "language": "javascript", "profile_id": profile_id,
        "steps": [{"id": "released-ui-contract", "phase": "test", "argv": ARGV,
                   "cwd": ".", "timeout_seconds": profile["timeout_seconds"]}],
        "outputs": [{key: item[key] for key in ("id", "kind", "path", "format", "required")}
                    | {"source_root": "out", "publish": "git_if_fits"} for item in outputs],
        "quality_gate": {"test_required": True, "minimum_executed": 1,
                         "required_outputs": ["junit", "ui-result"]}}}}
    wrapper_raw = (HERE / "run-ui-contract.cjs").read_bytes()
    release["adapter_sha256"] = digest(wrapper_raw)
    release["compose_sha256"] = digest(compose_raw)
    profile_raw = encoded(profile)
    grant = {"schema_version": "cdr-compose-grant/1",
             "profile_revision_sha256": {profile_id: digest(profile_raw)}}
    files = {
        project_dir / "compose.json": compose_raw,
        project_dir / "run-ui-contract.cjs": wrapper_raw,
        project_dir / "release-provenance.json": encoded(release),
        project_dir / ".runner/project.json": encoded(project),
        profile_dir / (profile_id + ".json"): profile_raw,
        profile_dir / "compose-grant.json": encoded(grant),
    }
    # Preparation is explicit enrollment material, never a runtime-config mutation.
    # On an I/O error retain partial files for inspection; do not guess what to delete.
    project_dir.mkdir(mode=0o700)
    profile_dir.mkdir(mode=0o700)
    (project_dir / ".runner").mkdir(mode=0o700)
    for path, raw in files.items():
        with path.open("xb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
    return {"project_dir": str(project_dir), "profile_dir": str(profile_dir),
            "project_id": project_id, "profile_id": profile_id, "backend_id": "rootless-compose",
            "profile_revision_sha256": digest(profile_raw), "compose_sha256": digest(compose_raw),
            "execution_timeout_seconds": 800, "images": release["images"],
            "executed": False, "runtime_configuration_changed": False}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-manifest", type=Path, required=True)
    parser.add_argument("--project-dir", type=Path, required=True)
    parser.add_argument("--profile-dir", type=Path, required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--profile-id", default="analytics-cc-ui")
    args = parser.parse_args()
    try:
        result = prepare(args.release_manifest, args.project_dir, args.profile_dir,
                         args.project_id, args.profile_id)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f"Preparation refused: {exc}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
