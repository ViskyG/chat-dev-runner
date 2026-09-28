#!/usr/bin/env python3
"""Install the checked-in python-basic PREPARATION/TEST pair for this rootless host.

The package wheels are already part of the starter and are hash-checked before any
owner profile is installed. No project code or dependency installer runs on the host.
The finite VERIFY handler still needs fresh hard-quota PREPARATION/TEST volumes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from runner_app.executors.broker import RootlessDockerBroker
from runner_app.executors.profiles import ProfileRegistry
from runner_app.git.templates import _python_quick_assets
from runner_app.storage.paths import (
    Paths,
    ensure_private_directory,
    open_private_file,
)

PROFILE_IDS = ("python-prepare", "python-quick")
IMAGE = "docker.io/library/python@sha256:4c47124a8391cb7a9f571164147d154777cf012a4ece5f86097130d7a4478111"


def _profile_bytes(profile_id: str) -> bytes:
    path = PROJECT_ROOT / "config" / f"profile.{profile_id}.json"
    reference: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if (reference.get("profile_id") != profile_id or reference.get("image") != IMAGE
            or reference.get("installed") is not False
            or reference.get("approved") is not False):
        raise ValueError("Checked-in reference profile differs from the reviewed install recipe")
    reference["installed"] = True
    reference["approved"] = True
    return (json.dumps(reference, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False) + "\n").encode("utf-8")


def _exact_private_file(path: Path, raw: bytes) -> None:
    """Create once, or accept an exact previous installation; never overwrite."""
    ensure_private_directory(path.parent)
    descriptor = open_private_file(path, create=True)
    try:
        old = os.read(descriptor, len(raw) + 1)
        if old:
            if old != raw:
                raise ValueError("Existing owner profile/receipt has different bytes")
            return
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("Owner profile write made no progress")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def install(profile_dir: Path, *, apply: bool) -> dict[str, object]:
    if not profile_dir.is_absolute() or ".." in profile_dir.parts:
        raise ValueError("Owner profile directory must be absolute")
    starter = _python_quick_assets()
    hashes = {profile_id: hashlib.sha256(_profile_bytes(profile_id)).hexdigest()
              for profile_id in PROFILE_IDS}
    with tempfile.TemporaryDirectory(prefix="cdr-python-quick-profiles-", dir="/tmp") as name:
        staged = Path(name)
        staged.chmod(0o700)
        for profile_id in PROFILE_IDS:
            _exact_private_file(staged / f"{profile_id}.json", _profile_bytes(profile_id))
        registry = ProfileRegistry(staged, hashes)
        prepare = registry.require("python-prepare")
        test = registry.require("python-quick")
        if (prepare.image != test.image or prepare.image != IMAGE
                or prepare.platform != test.platform or prepare.network != "none"
                or test.network != "none" or prepare.user != "0:0" or test.user != "0:0"
                or "/build" not in test.argv[2]
                or not any(item.id == "junit" and item.required for item in test.collect)):
            raise ValueError("Preparation/test profiles do not match the offline starter")
        broker = RootlessDockerBroker(registry, runtime_dir=Path(f"/run/user/{os.getuid()}"))
        probe = broker.probe()
        if not probe.available:
            raise ValueError(f"User-owned rootless Docker is unavailable: {probe.code}")
        image_id = broker._image_id(test)  # Exact installed RepoDigest and architecture.
    result: dict[str, object] = {
        "schema_version": "1.0", "installed": apply, "image": IMAGE,
        "image_id": image_id, "platform": test.platform,
        "rootless_probe": probe.code, "profile_dir": str(profile_dir),
        "profile_revision_sha256": hashes,
        "starter_wheel_count": len([name for name in starter if name.endswith(".whl")]),
        "starter_lockfile_sha256": hashlib.sha256(starter["requirements.lock"]).hexdigest(),
        "preparation_grant": {
            "template_id": "python-basic", "execution_profile_id": "python-quick",
            "execution_revision_sha256": hashes["python-quick"],
            "preparation_profile_id": "python-prepare",
            "preparation_revision_sha256": hashes["python-prepare"],
            "lockfile_path": "requirements.lock",
        },
    }
    if apply:
        ensure_private_directory(profile_dir)
        for profile_id in PROFILE_IDS:
            _exact_private_file(profile_dir / f"{profile_id}.json", _profile_bytes(profile_id))
        _exact_private_file(
            profile_dir / "python-quick-grant.json",
            (json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n").encode(),
        )
        installed = ProfileRegistry(profile_dir, hashes)
        if installed.available_ids() != frozenset(PROFILE_IDS):
            raise ValueError("Installed owner profiles failed readback")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-dir", type=Path,
                        default=Paths.discover().state_dir / "profiles")
    parser.add_argument("--install", action="store_true",
                        help="approve and write owner-private 0600 profiles after rootless checks")
    args = parser.parse_args()
    print(json.dumps(install(args.profile_dir, apply=args.install), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
