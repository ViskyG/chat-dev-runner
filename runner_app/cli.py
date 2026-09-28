"""Local foundation commands; inspect/dry-run never install or authenticate anything."""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from runner_app import __version__
from runner_app.adapters.drive_auth import DriveAuthError
from runner_app.adapters.drive_docs import DriveError
from runner_app.config import Settings, setup_requirements
from runner_app.storage.paths import Paths


def _tool_version(name: str, *args: str) -> dict[str, object]:
    executable = shutil.which(name)
    if executable is None:
        return {"status": "MISSING"}
    try:
        result = subprocess.run([executable, *args], capture_output=True, text=True, timeout=5,
                                check=False)
    except (OSError, subprocess.TimeoutExpired):
        return {"status": "UNAVAILABLE"}
    return {"status": "PRESENT" if result.returncode == 0 else "UNAVAILABLE",
            "version": result.stdout.strip()[:200], "exit_code": result.returncode}


def offline_report() -> dict[str, Any]:
    paths = Paths.discover()
    rootless = Path(f"/run/user/{os.getuid()}/docker.sock")
    try:
        info = rootless.lstat()
        candidate = stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid()
    except OSError:
        candidate = False
    return {
        "version": __version__, "mode": "OFFLINE", "network_calls": False,
        "status": "NOT_CONFIGURED", "armed": False,
        "host": {"python": platform.python_version(), "os": platform.system(),
                 "machine": platform.machine(), "git": _tool_version("git", "--version"),
                 "node": _tool_version("node", "--version"),
                 "docker_cli": _tool_version("docker", "--version"),
                 "cgroup_v2": Path("/sys/fs/cgroup/cgroup.controllers").is_file()},
        "paths": {"config": str(paths.config_dir), "state": str(paths.state_dir),
                  "cache": str(paths.cache_dir), "tokens": str(paths.token_dir)},
        "sandbox": {"status": "NOT_VERIFIED" if candidate else "NOT_CONFIGURED",
                    "local_rootless_socket_present": candidate,
                    "probe_performed": False, "host_execution_fallback": False},
        "setup": setup_requirements(),
        "missing_prerequisites": [
            "Rootless execution broker and containment acceptance",
            "Approved profiles, dependency build locks and local import policy",
            "Google and GitHub authorization and actual integration probes",
            "EP-02 through EP-07 implementation and release gates",
        ],
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="runner", description=__doc__)
    result.add_argument("--version", action="version", version=__version__)
    commands = result.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="Inspect prerequisites without external probes")
    doctor.add_argument("--offline", action="store_true", required=True)
    install = commands.add_parser("install", help="Report the installation plan only")
    install.add_argument("--dry-run", action="store_true", required=True)
    configure = commands.add_parser("configure", help="Describe local setup and authorization gates")
    configure.add_argument("--show-required", action="store_true", required=True)
    drive_check = commands.add_parser("drive-check", help="Validate the existing Drive installation")
    drive_check.add_argument("--live", action="store_true", required=True,
                             help="Explicitly use local OAuth for exact-ID read-only checks")
    serve = commands.add_parser("serve", help="Run the unarmed authenticated local API")
    serve.add_argument("--health-only", action="store_true", help="Stateless foundation probe")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--start-unarmed", action="store_true", default=True)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "serve":
            config = Settings(listen_host=args.host, listen_port=args.port)
            import uvicorn

            from runner_app.api.app import create_app

            if args.health_only:
                uvicorn.run(create_app(config), host=config.listen_host, port=config.listen_port,
                            workers=1, proxy_headers=False, access_log=False, server_header=False)
            else:
                from runner_app.engine.runtime import Runtime

                runtime = Runtime(config, Paths.discover())
                bridge = None
                try:
                    if runtime.integrations is not None:
                        from runner_app.adapters.drive_poller import DrivePoller, FrozenSlotStore
                        from runner_app.adapters.drive_system_state import SystemStatePublisher
                        from runner_app.engine.drive_bridge import DrivePollingBridge

                        integrations = runtime.integrations
                        root = integrations.attached.root_mailbox()
                        poller = DrivePoller(
                            integrations.provider, root, runtime.engine,
                            FrozenSlotStore(runtime.paths.state_dir / "drive-frozen-root"),
                        )
                        state_publisher = SystemStatePublisher(
                            integrations.root_receipt.writer, integrations.attached,
                            github_owner=integrations.config.github_login,
                            github_identity=integrations.config.github_identity,
                            approved_templates=runtime.engine.policy.approved_templates,
                            approved_profiles=lambda: runtime.engine.policy.approved_profiles,
                            available_actions=runtime.engine.capabilities,
                        )
                        bridge = DrivePollingBridge(poller, state_publisher=state_publisher.publish)
                        runtime.drive_bridge = bridge
                        bridge.start()
                        if runtime.workspace_mailboxes is not None:
                            runtime.workspace_mailboxes.start()
                    uvicorn.run(runtime.app, host=config.listen_host, port=config.listen_port,
                                workers=1, proxy_headers=False, access_log=False, server_header=False)
                finally:
                    if bridge is not None:
                        bridge.close()
                    runtime.close()
            return 0
        if args.command == "configure":
            report = setup_requirements()
        elif args.command == "drive-check":
            from runner_app.adapters.drive_check import live_drive_check

            report = live_drive_check()
        else:
            report = offline_report()
            if args.command == "install":
                report.update({"mode": "DRY_RUN", "installable": False,
                               "service_changes": False, "cloud_writes": False,
                               "would_start_armed": False})
        print(json.dumps(report, indent=2))
        return 0
    except (ValidationError, ValueError) as exc:
        if args.command == "drive-check":
            # Only stable adapter codes cross the CLI boundary.
            code = exc.code if isinstance(exc, (DriveAuthError, DriveError)) else "DRIVE_CHECK_FAILED"
            print(json.dumps({"error": code}))
            return 2
        # Local configuration validation only; never echo credential material.
        print(json.dumps({"error": "INVALID_LOCAL_CONFIGURATION", "detail": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
