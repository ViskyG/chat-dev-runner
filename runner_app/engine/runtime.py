"""Explicit local process startup, private session handoff and conservative recovery."""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from fastapi import FastAPI

from runner_app.adapters.drive_workspace_receipt import WorkspaceTerminalReceiptPublisher
from runner_app.api.auth import Sessions
from runner_app.api.runtime import create_runtime_app
from runner_app.config import DRIVE_INDEX_ID, DRIVE_ROOT_ID, Settings
from runner_app.config_v2 import load_v2_config
from runner_app.domain.commands import PROJECT_ACTIONS
from runner_app.engine.connection_checks import (
    ConnectionChecks,
    check_existing_drive,
    check_github_identity,
    check_registered_git_remote,
)
from runner_app.engine.execution_assembly import build_owner_execution_ports
from runner_app.engine.full_route import ExecutionPorts, make_full_route_factory
from runner_app.engine.integrations import (
    IntegrationConfigError,
    RuntimeIntegrations,
    build_runtime_integrations,
    load_owner_runtime_config,
)
from runner_app.engine.report_route import PublishReportRoute
from runner_app.engine.service import Engine
from runner_app.engine.singleton import Singleton
from runner_app.engine.workspace_mailboxes import RouteFactory, WorkspaceMailboxManager
from runner_app.git.imports import ImportGrants
from runner_app.git.registry import SourceRegistry
from runner_app.storage import Database, Paths, open_private_file
from runner_app.storage.ledger import Ledger, LedgerError
from runner_app.storage.run_evidence import RunEvidenceStore

_LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    from runner_app.engine.runtime_v2 import V2Assembly


class _DriveBridge(Protocol):
    def request_stop(self) -> None: ...
    def close(self, *, deadline: float | None = None) -> None: ...


class Runtime:
    v2: V2Assembly | None = None
    sessions: Sessions
    _bootstrap_path: Path
    _bootstrap_stop: threading.Event
    _bootstrap_refresh: threading.Event
    _bootstrap_thread: threading.Thread | None

    def _publish_bootstrap(self, secret: str) -> None:
        """Replace the owner-only handoff atomically, without a partial JSON read."""
        descriptor = open_private_file(self._bootstrap_path, create=True)
        os.close(descriptor)
        temporary = self._bootstrap_path.with_name(
            f".session-bootstrap-{secrets.token_hex(16)}.tmp"
        )
        try:
            descriptor = open_private_file(temporary, create=True)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(json.dumps({
                    "bootstrap": secret, "expires_in_seconds": self.sessions.bootstrap_ttl,
                }).encode())
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._bootstrap_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _rotate_bootstrap_loop(self) -> None:
        while not self._bootstrap_stop.is_set():
            interval = max(0.1, min(60.0, self.sessions.bootstrap_ttl * 0.75))
            self._bootstrap_refresh.wait(timeout=interval)
            self._bootstrap_refresh.clear()
            if self._bootstrap_stop.is_set():
                break
            try:
                self.sessions.issue_bootstrap(self._publish_bootstrap)
            except (OSError, ValueError):
                # Do not include paths, tokens or exception text in service logs.
                _LOGGER.error("SESSION_BOOTSTRAP_ROTATION_FAILED")

    def _start_bootstrap_rotation(self) -> None:
        if self._bootstrap_thread is None:
            thread = threading.Thread(
                target=self._rotate_bootstrap_loop, name="cdr-bootstrap-rotation", daemon=True,
            )
            self._bootstrap_thread = thread
            thread.start()

    def __init__(self, settings: Settings, paths: Paths, *,
                 import_roots: Sequence[Path] = (),
                 workspace_route_factory: RouteFactory | None = None,
                 execution_ports: ExecutionPorts | None = None) -> None:
        paths.ensure_private()
        self.paths = paths
        self.drive_bridge: _DriveBridge | None = None
        self.v2: V2Assembly | None = None
        self._singleton = Singleton(paths.state_dir / "runner.lock")
        self._bootstrap_path = paths.token_dir / "session-bootstrap.json"
        self._bootstrap_stop = threading.Event()
        self._bootstrap_refresh = threading.Event()
        self._bootstrap_thread = None
        try:
            v2_config = load_v2_config(paths)
            self.ledger = Ledger(Database(paths.database_path),
                                 runner_id=v2_config.runner_id if v2_config is not None else "local-runner",
                                 max_parallel=settings.max_parallel_executions,
                                 single_execution_lane=v2_config is not None)
            self.ledger.startup()
            owner_config = (load_owner_runtime_config(paths)
                            if v2_config is None or v2_config.enable_v1 else None)
            if owner_config is not None and import_roots:
                raise IntegrationConfigError("RUNTIME_IMPORT_ROOTS_AMBIGUOUS")
            if (owner_config is not None
                    and (settings.drive_root_id != DRIVE_ROOT_ID
                         or settings.drive_index_id != DRIVE_INDEX_ID)):
                raise IntegrationConfigError("RUNTIME_INSTALLATION_ID_MISMATCH")
            self.import_grants = ImportGrants(
                paths.state_dir / "import-handles",
                roots=owner_config.import_roots if owner_config is not None else import_roots,
            )
            self.source_registry = SourceRegistry(self.ledger.database,
                                                  paths.state_dir / "managed")
            self.run_evidence = RunEvidenceStore(self.ledger.database, self.source_registry,
                                                 paths.evidence_dir)
            self.integrations: RuntimeIntegrations | None = (
                build_runtime_integrations(
                    paths, self.ledger, self.source_registry, self.import_grants,
                    owner_config,
                ) if owner_config is not None else None
            )
            self.engine = Engine(
                self.ledger,
                policy=self.integrations.policy if self.integrations is not None else None,
                handlers=dict(self.integrations.handlers) if self.integrations is not None else None,
                import_grants=self.import_grants,
            )
            if self.integrations is not None:
                self.engine.handlers["PUBLISH_REPORT"] = PublishReportRoute(
                    self.integrations, self.ledger, self.source_registry,
                    self.run_evidence, paths,
                )
            if workspace_route_factory is not None and execution_ports is not None:
                raise IntegrationConfigError("RUNTIME_WORKSPACE_ROUTE_AMBIGUOUS")
            if ((workspace_route_factory is not None or execution_ports is not None)
                    and self.integrations is None):
                raise IntegrationConfigError("RUNTIME_WORKSPACE_ROUTE_UNCONFIGURED")
            self.execution_readiness_code = "NOT_CONFIGURED"
            if (execution_ports is None and workspace_route_factory is None
                    and self.integrations is not None
                    and self.integrations.config.execution_profile_dir is not None):
                assembly = build_owner_execution_ports(
                    paths, self.ledger, self.source_registry, self.run_evidence,
                    self.integrations.config.execution_profile_dir,
                )
                execution_ports = assembly.ports
                self.execution_readiness_code = assembly.code
            elif execution_ports is not None:
                self.execution_readiness_code = "INJECTED"
            route_factory = workspace_route_factory
            if execution_ports is not None:
                assert self.integrations is not None
                route_factory = make_full_route_factory(
                    self.integrations, self.ledger, self.source_registry,
                    paths, execution_ports,
                )
            self.workspace_mailboxes: WorkspaceMailboxManager | None = (
                WorkspaceMailboxManager(
                    self.ledger, self.source_registry,
                    self.integrations.drive_registry, self.integrations.provider,
                    self.engine, paths.state_dir / "drive-frozen-workspaces",
                    self.integrations.config.template_profiles,
                    route_factory=route_factory,
                    project_receipt=self.integrations.project_receipt,
                    workspace_receipt=WorkspaceTerminalReceiptPublisher(
                        self.ledger, self.integrations.attached,
                        self.integrations.provider, self.integrations.drive_registry,
                    ),
                ) if self.integrations is not None else None
            )
            connections = self.integrations
            self.connection_checks: ConnectionChecks | None = (
                ConnectionChecks(
                    drive=lambda: check_existing_drive(connections),
                    github_api=lambda: check_github_identity(connections),
                    git_remote=lambda project_id, workspace_id: check_registered_git_remote(
                        connections, self.source_registry, project_id, workspace_id,
                    ),
                ) if connections is not None else None
            )
            if v2_config is not None:
                from runner_app.engine.runtime_v2 import V2Assembly

                self.v2 = V2Assembly(v2_config, paths, self.ledger)
            self.sessions = Sessions()
            self.sessions.issue_bootstrap(self._publish_bootstrap)
            self.app: FastAPI = create_runtime_app(settings, self.engine, self.sessions,
                                                   import_grants=self.import_grants,
                                                   source_registry=self.source_registry,
                                                   drive_registry=(self.integrations.drive_registry
                                                                   if self.integrations is not None
                                                                   else None),
                                                   connection_checks=self.connection_checks,
                                                   run_evidence=self.run_evidence,
                                                   on_session_exchange=self._bootstrap_refresh.set,
                                                   integration_status=(
                                                       {
                                                           "state": "EXISTING_INSTALLATION_ATTACHED",
                                                           "drive_root_id": self.integrations.attached.config.root_id,
                                                           "drive_index_id": self.integrations.attached.config.index_id,
                                                           "github_login": self.integrations.config.github_login,
                                                           "github_user_id": self.integrations.config.github_user_id,
                                                           "draft_pr_capability": "NOT_PROBED",
                                                           "execution_readiness": self.execution_readiness_code,
                                                       } if self.integrations is not None else (
                                                           self.v2.status() if self.v2 is not None else None
                                                       )
                                                   ))
            if self.v2 is not None:
                self.v2.attach_api(self.app)
            # Uvicorn re-raises SIGTERM after completing its ASGI shutdown. Run cleanup
            # during the shutdown event, before that signal bypasses the CLI finally block.
            self.app.router.add_event_handler("startup", self._start_bootstrap_rotation)
            self.app.router.add_event_handler("shutdown", self.close)
        except BaseException:
            self._singleton.close()
            raise

    def drive_actions(self) -> list[str]:
        """Advertise only actions with a running exact Drive mailbox route."""
        actions: set[str] = set()
        if self.drive_bridge is not None and self.integrations is not None:
            actions.add("CREATE_PROJECT")
        manager = self.workspace_mailboxes
        if manager is not None:
            if manager.project_active_count():
                actions.update(PROJECT_ACTIONS)
            if manager.active_count():
                actions.update(action for action in ("RETARGET_WORKSPACE", "EXPORT_CONTEXT", "PUBLISH_REPORT")
                               if action in self.engine.handlers)
            if manager.full_route_count():
                actions.add("DELIVER_VERIFY_PUBLISH")
        return sorted(actions)

    def arm_on_startup(self) -> bool:
        """Enable a configured service after its Drive polling routes have started.

        Ledger.arm keeps interrupted or uncertain operations disarmed until the
        owner reconciles them. An unconfigured local foundation stays inert.
        """
        v1_ready = (self.integrations is not None and self.drive_bridge is not None
                    and self.workspace_mailboxes is not None)
        if (not v1_ready and not (self.v2 is not None and self.v2.configured)
                or self.engine.storage_faulted):
            return False
        try:
            self.ledger.arm()
        except LedgerError as error:
            if error.code != "NEEDS_RECONCILIATION":
                raise
            _LOGGER.warning("RUNNER_STARTUP_NEEDS_RECONCILIATION")
            return False
        return True

    def close(self) -> None:
        # The user unit allows 30 seconds. Signal every poller first, then share
        # one 20-second budget across all joins instead of 30 seconds per route.
        deadline = time.monotonic() + 20.0
        self._bootstrap_stop.set()
        self._bootstrap_refresh.set()
        manager = self.workspace_mailboxes
        bridge = self.drive_bridge
        if self.v2 is not None:
            self.v2.request_stop()
        if manager is not None:
            manager.request_stop()
        if bridge is not None:
            bridge.request_stop()
        if self._bootstrap_thread is not None:
            self._bootstrap_thread.join(timeout=max(0.0, deadline - time.monotonic()))
            if not self._bootstrap_thread.is_alive():
                self._bootstrap_thread = None
        try:
            try:
                if manager is not None:
                    manager.close(deadline=deadline)
                if bridge is not None:
                    bridge.close(deadline=deadline)
                if self.v2 is not None:
                    self.v2.close(deadline=deadline)
            finally:
                self.engine.shutdown(timeout=min(2.0, max(0.0, deadline - time.monotonic())))
                self._bootstrap_path.unlink(missing_ok=True)
        finally:
            self._singleton.close()
