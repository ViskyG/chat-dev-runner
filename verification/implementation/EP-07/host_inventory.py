"""Read-only EP-07 host inventory; records no credential or file contents."""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PORT = 8765
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._()/+:-]{0,119}\Z")


def version(name: str, *args: str) -> dict[str, Any]:
    executable = shutil.which(name)
    if executable is None:
        return {"status": "MISSING"}
    try:
        result = subprocess.run([executable, *args], capture_output=True,
                                text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return {"status": "UNAVAILABLE"}
    first = result.stdout.splitlines()[0].strip() if result.stdout.splitlines() else ""
    return {"status": "PRESENT" if result.returncode == 0 else "UNAVAILABLE",
            "version": first if _VERSION.fullmatch(first) else None}


def metadata(path: Path, *, kind: str) -> dict[str, Any]:
    try:
        entry = path.lstat()
    except FileNotFoundError:
        return {"present": False}
    expected = stat.S_ISSOCK if kind == "socket" else stat.S_ISREG
    return {"present": True, "kind_matches": expected(entry.st_mode),
            "owner_is_current_user": entry.st_uid == os.getuid(),
            "owner_is_root": entry.st_uid == 0,
            "mode": format(stat.S_IMODE(entry.st_mode), "04o")}


def listeners(port: int) -> dict[str, Any]:
    executable = shutil.which("ss")
    if executable is None:
        return {"status": "UNAVAILABLE", "binds": []}
    try:
        result = subprocess.run([executable, "-H", "-ltn"], capture_output=True,
                                text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return {"status": "UNAVAILABLE", "binds": []}
    if result.returncode:
        return {"status": "UNAVAILABLE", "binds": []}
    binds: list[str] = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        local = fields[3]
        if local.rsplit(":", 1)[-1] == str(port):
            binds.append(local)
    return {"status": "OBSERVED", "binds": sorted(binds),
            "loopback_only": all(value.startswith(("127.", "[::1]:"))
                                 for value in binds)}


def main() -> None:
    uid = os.getuid()
    report = {
        "schema_version": "1.0", "packet": "EP-07",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "mode": "READ_ONLY_HOST_INVENTORY", "external_writes": False,
        "host": {"python": platform.python_version(), "system": platform.system(),
                 "kernel": platform.release(), "machine": platform.machine(),
                 "cgroup_v2": Path("/sys/fs/cgroup/cgroup.controllers").is_file()},
        "tools": {"git": version("git", "--version"),
                  "docker": version("docker", "--version"),
                  "systemctl": version("systemctl", "--version"),
                  "node": version("node", "--version"),
                  "code": version("code", "--version")},
        "user_systemd_bus": metadata(Path(f"/run/user/{uid}/bus"), kind="socket"),
        "rootless_docker_socket": metadata(Path(f"/run/user/{uid}/docker.sock"), kind="socket"),
        "quota_helper": metadata(Path("/usr/local/libexec/chat-dev-runner-volume-helper"), kind="file"),
        "runner_port_before_install": listeners(PORT),
    }
    destination = Path(__file__).with_name("host-inventory.json")
    destination.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"result": "RECORDED", "evidence": str(destination),
                      "runner_port_loopback_only": report["runner_port_before_install"].get("loopback_only")}))


if __name__ == "__main__":
    main()
