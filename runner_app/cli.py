"""Owner-local runner commands; inspection never installs or authenticates."""
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
from runner_app.service_install import (
    ServiceInstallError,
    dry_run,
    inspect_runtime_package,
    install_user,
    render_plan,
    status_user,
    stop_user,
    uninstall_user,
)
from runner_app.storage.backup import BackupError
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
    setup = setup_requirements()
    setup["state"] = "CONFIGURATION_NOT_PROBED"
    setup["armed"] = None
    setup["ready_commands"] = None
    for provider in ("google", "github"):
        details = setup[provider]
        assert isinstance(details, dict)
        details["status"] = "NOT_PROBED"
    return {
        "version": __version__, "mode": "OFFLINE", "network_calls": False,
        "status": "CONFIGURATION_NOT_PROBED", "armed": None,
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
        "setup": setup,
        "missing_prerequisites": [
            "EP-07 installed user-service host acceptance and signed release gate",
        ],
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="runner", description=__doc__)
    result.add_argument("--version", action="version", version=__version__)
    commands = result.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="Inspect prerequisites without external probes")
    doctor.add_argument("--offline", action="store_true", required=True)
    install = commands.add_parser("install", help="Inspect or install the owner-user service")
    install_mode = install.add_mutually_exclusive_group(required=True)
    install_mode.add_argument("--dry-run", action="store_true")
    install_mode.add_argument("--user", action="store_true")
    install.add_argument("--port", type=int, default=8765)
    commands.add_parser("status", help="Inspect the owner-user service")
    commands.add_parser("stop", help="Stop the owner-user service")
    uninstall = commands.add_parser("uninstall", help="Remove only the managed user service")
    uninstall.add_argument("--user", action="store_true", required=True)
    backup = commands.add_parser("backup", help="Snapshot stopped durable state without secrets")
    backup.add_argument("--output", type=Path, required=True)
    restore = commands.add_parser("restore", help="Restore an archive to its absent original state path")
    restore.add_argument("--from", dest="archive", type=Path, required=True)
    configure = commands.add_parser("configure", help="Describe local setup and authorization gates")
    configure.add_argument("--show-required", action="store_true", required=True)
    drive_check = commands.add_parser("drive-check", help="Validate the existing Drive installation")
    drive_check.add_argument("--live", action="store_true", required=True,
                             help="Explicitly use local OAuth for exact-ID read-only checks")
    serve = commands.add_parser("serve", help="Run the authenticated local API")
    serve.add_argument("--health-only", action="store_true", help="Stateless foundation probe")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--start-unarmed", action="store_true",
                       help="Keep command intake off for this process")
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
                            available_actions=runtime.drive_actions,
                        )
                        bridge = DrivePollingBridge(
                            poller, state_publisher=state_publisher.publish,
                            available_actions=runtime.drive_actions,
                        )
                        runtime.drive_bridge = bridge
                        bridge.start()
                        if runtime.workspace_mailboxes is not None:
                            runtime.workspace_mailboxes.start()
                    if runtime.v2 is not None:
                        runtime.v2.start()
                    if not args.start_unarmed:
                        runtime.arm_on_startup()
                    uvicorn.run(runtime.app, host=config.listen_host, port=config.listen_port,
                                workers=1, proxy_headers=False, access_log=False, server_header=False)
                finally:
                    if bridge is not None:
                        bridge.close()
                    runtime.close()
            return 0
        if args.command == "install":
            plan = render_plan(port=args.port)
            if args.dry_run:
                report = offline_report()
                report.update(dry_run(plan))
            else:
                package = inspect_runtime_package(plan.executable)
                if package["status"] != "WHEEL":
                    raise ServiceInstallError("WHEEL_INSTALL_REQUIRED", runtime_package=package)
                report = {"mode": "USER_INSTALL", **install_user(plan)}
        elif args.command == "status":
            report = status_user(render_plan())
        elif args.command == "stop":
            report = stop_user(render_plan())
        elif args.command == "uninstall":
            report = uninstall_user(render_plan())
        elif args.command == "backup":
            from runner_app.storage.backup import create_backup

            report = {"mode": "BACKUP", **create_backup(Paths.discover(), args.output)}
        elif args.command == "restore":
            from runner_app.storage.backup import restore_backup

            report = {"mode": "RESTORE", **restore_backup(args.archive, Paths.discover().state_dir)}
        elif args.command == "configure":
            report = setup_requirements()
        elif args.command == "drive-check":
            from runner_app.adapters.drive_check import live_drive_check

            report = live_drive_check()
        else:
            report = offline_report()
        print(json.dumps(report, indent=2))
        return 0
    except ServiceInstallError as exc:
        print(json.dumps({"error": exc.code, **exc.details}))
        return 2
    except BackupError as exc:
        print(json.dumps({"error": exc.code}))
        return 2
    except OSError:
        print(json.dumps({"error": "LOCAL_SERVICE_IO_FAILED"}))
        return 2
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
