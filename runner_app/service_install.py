"""Owner-user systemd unit, rendered from the installed package without credentials."""
from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

from runner_app.config import Settings
from runner_app.storage.paths import Paths, open_private_file

UNIT_NAME = "chat-dev-runner.service"
_MANAGED = "# Managed by Chat-Dev Runner user installer; do not hand-edit this file."
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


class ServiceInstallError(ValueError):
    def __init__(self, code: str, **details: Any) -> None:
        self.code = code
        self.details = details
        super().__init__(code)


def _unit_quote(value: str) -> str:
    """Quote one systemd value; percent specifiers must stay literal."""
    if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ServiceInstallError("UNSAFE_UNIT_VALUE")
    return json.dumps(value.replace("%", "%%"), ensure_ascii=False)


def _unit_path(value: str) -> str:
    """Escape a path for systemd path-valued settings such as WorkingDirectory."""
    if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ServiceInstallError("UNSAFE_UNIT_VALUE")
    return (value.replace("\\", "\\x5c").replace("%", "%%")
            .replace(" ", "\\x20").replace('"', "\\x22"))


def _absolute(path: Path) -> Path:
    if not path.is_absolute() or ".." in path.parts:
        raise ServiceInstallError("INVALID_SERVICE_PATH")
    return path


@dataclass(frozen=True)
class ServicePlan:
    paths: Paths
    unit_path: Path
    executable: Path
    port: int
    text: str

    @property
    def argv(self) -> list[str]:
        return [str(self.executable), "-m", "runner_app.cli", "serve", "--host",
                "127.0.0.1", "--port", str(self.port)]


def render_plan(paths: Paths | None = None, *, executable: Path | None = None,
                port: int = 8765) -> ServicePlan:
    selected = paths or Paths.discover()
    Settings(listen_port=port)  # loopback-only and bounded-port contract
    python = _absolute(executable or Path(sys.executable))
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ServiceInstallError("PYTHON_EXECUTABLE_UNAVAILABLE")
    for directory in (selected.config_dir, selected.state_dir, selected.cache_dir):
        _absolute(directory)
        if directory.name != "chat-dev-runner":
            raise ServiceInstallError("INVALID_SERVICE_PATH")
    if selected.runtime_dir is not None:
        _absolute(selected.runtime_dir)
        if selected.runtime_dir.name != "chat-dev-runner":
            raise ServiceInstallError("INVALID_SERVICE_PATH")
    unit_path = selected.config_dir.parent / "systemd" / "user" / UNIT_NAME
    template = files("runner_app").joinpath("production.service.in").read_text(encoding="utf-8")
    runtime = (f"Environment={_unit_quote('XDG_RUNTIME_DIR=' + str(selected.runtime_dir.parent))}"
               if selected.runtime_dir is not None else "")
    replacements = {
        "@STATE_DIR@": _unit_path(str(selected.state_dir)),
        "@VENV_PYTHON@": _unit_quote(str(python)),
        "@PORT@": str(port),
        "@XDG_CONFIG_HOME@": _unit_quote("XDG_CONFIG_HOME=" + str(selected.config_dir.parent)),
        "@XDG_DATA_HOME@": _unit_quote("XDG_DATA_HOME=" + str(selected.state_dir.parent)),
        "@XDG_CACHE_HOME@": _unit_quote("XDG_CACHE_HOME=" + str(selected.cache_dir.parent)),
        "@RUNTIME_ENV@": runtime,
    }
    rendered = template
    for marker, value in replacements.items():
        if rendered.count(marker) != 1:
            raise ServiceInstallError("SERVICE_TEMPLATE_INVALID")
        rendered = rendered.replace(marker, value)
    if re.search(r"@[A-Z_]+@", rendered) or not rendered.startswith(_MANAGED):
        raise ServiceInstallError("SERVICE_TEMPLATE_INVALID")
    return ServicePlan(selected, unit_path, python, port, rendered)


def _open_unit_directory(path: Path, *, create: bool) -> int:
    _absolute(path)
    descriptor = os.open("/", _DIRECTORY_FLAGS)
    try:
        for part in path.parts[1:]:
            try:
                child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022:
            raise ServiceInstallError("UNSAFE_UNIT_DIRECTORY")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_unit(directory_fd: int) -> bytes | None:
    try:
        descriptor = os.open(UNIT_NAME, _FILE_FLAGS, dir_fd=directory_fd)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o022
                or info.st_size > 16_384):
            raise ServiceInstallError("UNSAFE_UNIT_FILE")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return stream.read(16_385)
    finally:
        os.close(descriptor)


def _unit_state(plan: ServicePlan) -> str:
    try:
        directory_fd = _open_unit_directory(plan.unit_path.parent, create=False)
    except FileNotFoundError:
        return "ABSENT"
    try:
        current = _read_unit(directory_fd)
    finally:
        os.close(directory_fd)
    if current is None:
        return "ABSENT"
    if not current.startswith((_MANAGED + "\n").encode()):
        return "FOREIGN"
    return "CURRENT" if current == plan.text.encode() else "MANAGED_DIFFERENT"


def _write_unit(plan: ServicePlan) -> None:
    descriptor = _open_unit_directory(plan.unit_path.parent, create=True)
    temporary = f".{UNIT_NAME}.{os.getpid()}.tmp"
    try:
        current = _read_unit(descriptor)
        if current is not None and not current.startswith((_MANAGED + "\n").encode()):
            raise ServiceInstallError("UNMANAGED_UNIT_EXISTS")
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC
        target = os.open(temporary, flags, 0o600, dir_fd=descriptor)
        try:
            os.fchmod(target, 0o600)
            with os.fdopen(target, "wb", closefd=False) as stream:
                stream.write(plan.text.encode())
                stream.flush()
                os.fsync(target)
        finally:
            os.close(target)
        os.replace(temporary, UNIT_NAME, src_dir_fd=descriptor, dst_dir_fd=descriptor)
        os.fsync(descriptor)
    finally:
        try:
            os.unlink(temporary, dir_fd=descriptor)
        except FileNotFoundError:
            pass
        os.close(descriptor)


def _remove_unit(plan: ServicePlan) -> None:
    descriptor = _open_unit_directory(plan.unit_path.parent, create=False)
    try:
        current = _read_unit(descriptor)
        if current is None:
            return
        if not current.startswith((_MANAGED + "\n").encode()):
            raise ServiceInstallError("UNMANAGED_UNIT_EXISTS")
        os.unlink(UNIT_NAME, dir_fd=descriptor)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _systemctl(*arguments: str) -> int:
    try:
        result = subprocess.run(["systemctl", "--user", *arguments], capture_output=True,
                                text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ServiceInstallError("SYSTEMCTL_UNAVAILABLE") from error
    return result.returncode


def _fragment_path() -> str | None:
    """Read the exact currently loaded systemd unit file, if any."""
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show", "-p", "FragmentPath", "--value", UNIT_NAME],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ServiceInstallError("SYSTEMCTL_UNAVAILABLE") from error
    if result.returncode != 0:
        raise ServiceInstallError("SYSTEMCTL_FAILED")
    return result.stdout.strip() or None


def _reject_foreign_fragment(plan: ServicePlan) -> str | None:
    fragment = _fragment_path()
    if fragment is not None and fragment != str(plan.unit_path):
        raise ServiceInstallError("FOREIGN_SERVICE_FRAGMENT", fragment_path=fragment)
    return fragment


@contextmanager
def _lifecycle_lock(plan: ServicePlan) -> Iterator[None]:
    descriptor = open_private_file(plan.paths.config_dir / "service-lifecycle.lock", create=True)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ServiceInstallError("SERVICE_LIFECYCLE_BUSY") from error
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _require_systemctl(*arguments: str) -> None:
    if _systemctl(*arguments) != 0:
        raise ServiceInstallError("SYSTEMCTL_FAILED")


_PACKAGE_PROBE = """
import importlib.metadata as metadata
import importlib.util
import json
spec = importlib.util.find_spec('runner_app')
records = []
for distribution in metadata.distributions():
    if distribution.metadata.get('Name', '').lower().replace('_', '-') != 'chat-dev-runner':
        continue
    direct = distribution.read_text('direct_url.json')
    try:
        editable = bool(json.loads(direct).get('dir_info', {}).get('editable')) if direct else False
    except (ValueError, AttributeError):
        editable = False
    records.append((distribution.version, editable))
origin = spec.origin if spec is not None else None
editable = any(record[1] for record in records)
status = ('MISSING' if origin is None else
          'EDITABLE_SOURCE' if editable or '/site-packages/' not in origin else 'WHEEL')
print(json.dumps({'status': status, 'module_origin': origin,
                  'distribution_versions': sorted({record[0] for record in records})}))
"""


def inspect_runtime_package(executable: Path) -> dict[str, Any]:
    """Resolve imports as the service will, away from the source checkout."""
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        result = subprocess.run(
            [str(executable), "-I", "-c", _PACKAGE_PROBE], cwd="/", env=environment,
            capture_output=True, text=True, timeout=8, check=False,
        )
        if result.returncode != 0:
            raise ValueError("package probe failed")
        value = json.loads(result.stdout)
        if not isinstance(value, dict) or value.get("status") not in {
            "MISSING", "EDITABLE_SOURCE", "WHEEL",
        }:
            raise ValueError("package probe returned invalid result")
        return value
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {"status": "NOT_PROBED", "module_origin": None, "distribution_versions": []}


def dry_run(plan: ServicePlan) -> dict[str, Any]:
    try:
        unit_state = _unit_state(plan)
    except (ServiceInstallError, OSError):
        unit_state = "UNSAFE"
    runtime_package = inspect_runtime_package(plan.executable)
    return {
        "mode": "DRY_RUN", "installable": (
            unit_state not in {"FOREIGN", "UNSAFE"} and runtime_package["status"] == "WHEEL"
        ),
        "unit_state": unit_state, "unit_path": str(plan.unit_path), "argv": plan.argv,
        "runtime_package": runtime_package,
        "config_dir": str(plan.paths.config_dir), "state_dir": str(plan.paths.state_dir),
        "cache_dir": str(plan.paths.cache_dir), "service_changes": False,
        "cloud_writes": False, "would_start_armed": None,
        "startup_arm_policy": "CONFIGURED_RUNTIME",
        "live_start_may_publish_system_state": True,
        "ready_mailbox_polling_while_disarmed": False,
    }


def status_user(plan: ServicePlan) -> dict[str, Any]:
    fragment = _fragment_path()
    if fragment is not None and fragment != str(plan.unit_path):
        return {"unit_path": str(plan.unit_path), "unit_state": "FOREIGN_FRAGMENT",
                "fragment_path": fragment, "installed": False, "active": None, "enabled": None}
    state = _unit_state(plan)
    if state in {"FOREIGN", "ABSENT"}:
        return {"unit_path": str(plan.unit_path), "unit_state": state,
                "installed": False, "active": False, "enabled": False}
    return {"unit_path": str(plan.unit_path), "unit_state": state,
            "installed": True, "active": _systemctl("is-active", "--quiet", UNIT_NAME) == 0,
            "enabled": _systemctl("is-enabled", "--quiet", UNIT_NAME) == 0}


def _require_existing_state_root(plan: ServicePlan) -> None:
    """A managed unit cannot silently replace a lost state directory with a new one."""
    try:
        descriptor = _open_unit_directory(plan.paths.state_dir, create=False)
    except FileNotFoundError as error:
        raise ServiceInstallError("SERVICE_STATE_ROOT_MISSING") from error
    try:
        if stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o700:
            raise ServiceInstallError("SERVICE_STATE_ROOT_UNSAFE")
    finally:
        os.close(descriptor)


def _install_step(stage: str, plan: ServicePlan, changed: bool, *arguments: str) -> None:
    try:
        _require_systemctl(*arguments)
    except ServiceInstallError as error:
        # A nonzero systemctl result may follow a partial effect. Disable the
        # login hook best-effort, retain the exact unit, and require readback.
        disabled: bool | None = None
        if stage in {"ENABLE", "START"}:
            try:
                disabled = _systemctl("disable", UNIT_NAME) == 0
            except ServiceInstallError:
                disabled = False
        suffix = "PARTIAL_UNCERTAIN" if disabled is False else "FAILED"
        raise ServiceInstallError(
            f"SERVICE_INSTALL_{stage}_{suffix}", unit_path=str(plan.unit_path),
            unit_file_written=changed, recovery="RUN_STATUS_THEN_RETRY_INSTALL",
            service_may_have_started=stage == "START", disabled_after_failure=disabled,
        ) from error


def install_user(plan: ServicePlan) -> dict[str, Any]:
    _reject_foreign_fragment(plan)
    with _lifecycle_lock(plan):
        return _install_user_locked(plan)


def _install_user_locked(plan: ServicePlan) -> dict[str, Any]:
    _reject_foreign_fragment(plan)
    state = _unit_state(plan)
    if state == "FOREIGN":
        raise ServiceInstallError("UNMANAGED_UNIT_EXISTS")
    if state != "ABSENT":
        _require_existing_state_root(plan)
    active = _systemctl("is-active", "--quiet", UNIT_NAME) == 0
    enabled = _systemctl("is-enabled", "--quiet", UNIT_NAME) == 0
    if state == "ABSENT" and active:
        raise ServiceInstallError("SERVICE_NAME_ALREADY_ACTIVE")
    if state == "MANAGED_DIFFERENT" and active:
        raise ServiceInstallError("ACTIVE_SERVICE_UPGRADE_REQUIRES_STOP")
    changed = state != "CURRENT"
    if changed:
        # These private roots are created before systemd can start the process.
        if state == "ABSENT":
            plan.paths.ensure_private()
        _write_unit(plan)
    if changed or not active:
        # A prior reload may have failed after the unit file was written.
        _install_step("RELOAD", plan, changed, "daemon-reload")
    if not enabled:
        _install_step("ENABLE", plan, changed, "enable", UNIT_NAME)
    if not active:
        _install_step("START", plan, changed, "start", UNIT_NAME)
    return {"unit_path": str(plan.unit_path), "installed": True,
            "unit_changed": changed, "started": not active, "enabled": True,
            "startup_arm_policy": "CONFIGURED_RUNTIME",
            "live_start_may_publish_system_state": not active,
            "ready_mailbox_polling_while_disarmed": False}


def stop_user(plan: ServicePlan) -> dict[str, Any]:
    _reject_foreign_fragment(plan)
    with _lifecycle_lock(plan):
        return _stop_user_locked(plan)


def _stop_user_locked(plan: ServicePlan) -> dict[str, Any]:
    _reject_foreign_fragment(plan)
    state = _unit_state(plan)
    if state == "FOREIGN":
        raise ServiceInstallError("UNMANAGED_UNIT_EXISTS")
    if state == "ABSENT":
        return {"unit_path": str(plan.unit_path), "installed": False, "stopped": False}
    active = _systemctl("is-active", "--quiet", UNIT_NAME) == 0
    if active:
        _require_systemctl("stop", UNIT_NAME)
    return {"unit_path": str(plan.unit_path), "installed": True, "stopped": active}


def uninstall_user(plan: ServicePlan) -> dict[str, Any]:
    _reject_foreign_fragment(plan)
    with _lifecycle_lock(plan):
        return _uninstall_user_locked(plan)


def _uninstall_user_locked(plan: ServicePlan) -> dict[str, Any]:
    fragment = _reject_foreign_fragment(plan)
    state = _unit_state(plan)
    if state == "FOREIGN":
        raise ServiceInstallError("UNMANAGED_UNIT_EXISTS")
    if state == "ABSENT":
        # Reload only an exact stale managed fragment after interrupted removal.
        if fragment == str(plan.unit_path):
            _require_systemctl("daemon-reload")
        return {"unit_path": str(plan.unit_path), "unit_removed": False,
                "state_preserved": True, "cloud_resources_preserved": True}
    if _systemctl("is-active", "--quiet", UNIT_NAME) == 0:
        _require_systemctl("stop", UNIT_NAME)
    if _systemctl("is-enabled", "--quiet", UNIT_NAME) == 0:
        _require_systemctl("disable", UNIT_NAME)
    _remove_unit(plan)
    _require_systemctl("daemon-reload")
    return {"unit_path": str(plan.unit_path), "unit_removed": True,
            "state_preserved": True, "cloud_resources_preserved": True}
