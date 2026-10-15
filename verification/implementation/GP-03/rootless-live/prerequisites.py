"""Read-only host readiness; no allocations, service changes, workload or cloud calls."""
from __future__ import annotations

import argparse
import json
import os
import platform
import shlex
import sqlite3
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from runner_app.executors.broker import BrokerError, RootlessDockerBroker
from runner_app.executors.volume_provider import (
    HELPER,
    VOLUME_BASE,
    HardQuotaVolumeProvider,
    VolumeProviderError,
    load_python_quick_grant,
)

OWNER_HOME = Path("/home/visky")
OWNER_CONFIG = OWNER_HOME / ".config/chat-dev-runner/owner-runtime.json"
LIVE_DATABASE = OWNER_HOME / ".local/share/chat-dev-runner/state.sqlite"


def file_identity(path: Path) -> dict[str, Any]:
    try:
        info = path.lstat()
        return {"exists": True, "uid": info.st_uid, "gid": info.st_gid,
                "mode": f"{stat.S_IMODE(info.st_mode):04o}", "nlink": info.st_nlink,
                "regular_file": stat.S_ISREG(info.st_mode), "directory": stat.S_ISDIR(info.st_mode)}
    except OSError:
        return {"exists": False}


def service_state() -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["/usr/bin/systemctl", "--user", "show", "chat-dev-runner.service", "--no-pager",
             "--property=LoadState,ActiveState,SubState,MainPID,ExecMainStatus,Environment"],
            env={"PATH": "/usr/bin:/bin", "HOME": str(OWNER_HOME),
                 "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}",
                 "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{os.getuid()}/bus"},
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=5, check=False)
        allowed = {"LoadState", "ActiveState", "SubState", "MainPID", "ExecMainStatus"}
        values = dict(line.split("=", 1) for line in result.stdout.decode().splitlines()
                      if "=" in line and line.split("=", 1)[0] in allowed)
        environment = next((line.removeprefix("Environment=")
                            for line in result.stdout.decode().splitlines()
                            if line.startswith("Environment=")), "")
        for assignment in shlex.split(environment):
            if not assignment.startswith("XDG_DATA_HOME="):
                continue
            selected = Path(assignment.removeprefix("XDG_DATA_HOME="))
            if selected.is_absolute() and ".." not in selected.parts and selected.resolve() == selected:
                values["XDG_DATA_HOME"] = str(selected)
        return {"available": result.returncode == 0, **values}
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        return {"available": False}


def live_counts(database: Path = LIVE_DATABASE) -> dict[str, Any]:
    """Read only aggregate status columns; no command, effect intent or token payloads."""
    if not database.is_file():
        return {"available": False, "code": "LIVE_DATABASE_MISSING"}
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=2)
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            result: dict[str, Any] = {"available": True}
            result["operations_running"] = connection.execute(
                "SELECT COUNT(*) FROM operations WHERE status='RUNNING'").fetchone()[0]
            result["capacity_held"] = connection.execute(
                "SELECT COUNT(*) FROM operations WHERE capacity_held=1").fetchone()[0]
            result["reconcile_required"] = connection.execute(
                "SELECT COUNT(*) FROM operations WHERE reconcile_required=1").fetchone()[0]
            result["pending_effects"] = connection.execute(
                "SELECT COUNT(*) FROM effects WHERE state IN ('INTENDED','UNKNOWN')").fetchone()[0]
            result["armed"] = bool(connection.execute(
                "SELECT armed FROM runner_state WHERE singleton=1").fetchone()[0])
            if "execution_lane" in tables:
                result["execution_lane_count"] = connection.execute(
                    "SELECT COUNT(*) FROM execution_lane").fetchone()[0]
            if "v2_requests" in tables:
                result["v2_status_counts"] = dict(connection.execute(
                    "SELECT status,COUNT(*) FROM v2_requests GROUP BY status").fetchall())
            active = sum(result[key] for key in (
                "operations_running", "capacity_held", "reconcile_required", "pending_effects"))
            active += result.get("execution_lane_count", 0)
            active += sum(count for status, count in result.get("v2_status_counts", {}).items()
                          if status in {"ACK_PENDING", "ACK_UNKNOWN", "QUEUED", "STARTING", "RUNNING",
                                        "CONTROL", "CONTROL_RUNNING", "NEEDS_RECONCILIATION"})
            result["idle_without_pending_intent"] = active == 0
            return result
        finally:
            connection.close()
    except (sqlite3.Error, OSError, TypeError):
        return {"available": False, "code": "LIVE_DATABASE_READ_UNAVAILABLE"}


def profile_dir() -> Path:
    descriptor = os.open(OWNER_CONFIG, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 262144):
            raise ValueError("OWNER_CONFIG_UNSAFE")
        # Only this approved field is consumed; never output the config object.
        selected = json.loads(stream.read(262145)).get("execution_profile_dir")
    if not isinstance(selected, str):
        raise TypeError("EXECUTION_PROFILE_DIR_MISSING")
    path = Path(selected)
    if not path.is_absolute() or ".." in path.parts or path.resolve() != path:
        raise ValueError("EXECUTION_PROFILE_DIR_UNSAFE")
    return path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--context", choices=("host", "sandbox"), required=True)
    parser.add_argument("--out", type=Path, required=True)
    arguments = parser.parse_args()
    record: dict[str, Any] = {
        "scope": "read-only prerequisites, no workload or allocation",
        "context": arguments.context, "observed_at": datetime.now(timezone.utc).isoformat(),
        "uid": os.getuid(), "euid": os.geteuid(), "gid": os.getgid(),
        "os": platform.system(), "arch": platform.machine(),
        "helper": file_identity(HELPER), "helper_parent": file_identity(HELPER.parent),
        "installed_helper_safe": HardQuotaVolumeProvider._installed_helper(),
        "rootless_socket": file_identity(Path(f"/run/user/{os.getuid()}/docker.sock")),
        "workload": "NOT_RUN", "allocations": "NOT_ATTEMPTED", "service_changes": "NONE",
    }
    if arguments.context == "host":
        record["service"] = service_state()
        selected_data = record["service"].get("XDG_DATA_HOME")
        database = Path(selected_data) / "chat-dev-runner/state.sqlite" if selected_data else LIVE_DATABASE
        record["live_database_path"] = str(database)
        record["unit_environment_fields_consumed"] = ["XDG_DATA_HOME"]
        record["live_ledger"] = live_counts(database)
        record["owner_config_fields_consumed"] = ["execution_profile_dir"]
        try:
            selected = profile_dir()
            profiles, grant = load_python_quick_grant(selected)
            execution, preparation = profiles.require("python-quick"), profiles.require("python-prepare")
            record["profiles"] = {"available": True, "directory": str(selected),
                "execution_revision_sha256": execution.revision_sha256,
                "preparation_revision_sha256": preparation.revision_sha256,
                "image": execution.image, "platform": execution.platform,
                "grant_template": grant.template_id}
            broker = RootlessDockerBroker(profiles, runtime_dir=Path(f"/run/user/{os.getuid()}"))
            probe = broker.probe()
            record["rootless_runtime"] = {"available": probe.available, "code": probe.code,
                "version": probe.server_version, "cgroup_version": probe.cgroup_version,
                "cgroup_driver": probe.cgroup_driver, "controllers": probe.delegated_controllers}
            if probe.available:
                record["pinned_image"] = {"verified": True, "image_id": broker._image_id(execution)}
            if record["installed_helper_safe"]:
                response = HardQuotaVolumeProvider._helper("probe")
                record["helper_probe"] = {"ready": response == {
                    "status": "READY", "base": str(VOLUME_BASE), "max_slots": 8}}
        except (OSError, ValueError, TypeError, AttributeError, BrokerError, VolumeProviderError) as error:
            record["prerequisite_error"] = getattr(error, "code", "OWNER_PROFILE_UNAVAILABLE")
        try:
            record["existing_volume_directories"] = sum(1 for path in VOLUME_BASE.iterdir() if path.is_dir())
        except OSError:
            record["existing_volume_directories"] = None
        record["canary_prerequisites_ready"] = bool(
            record["installed_helper_safe"] and record.get("profiles", {}).get("available")
            and record.get("rootless_runtime", {}).get("available")
            and record.get("pinned_image", {}).get("verified")
            and record.get("helper_probe", {}).get("ready")
            and record["live_ledger"].get("idle_without_pending_intent")
            and record["service"].get("available"))
    arguments.out.parent.mkdir(parents=True, exist_ok=True)
    arguments.out.write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record, sort_keys=True))


if __name__ == "__main__":
    main()
