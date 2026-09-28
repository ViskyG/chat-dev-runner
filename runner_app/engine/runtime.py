"""Explicit local process startup, private session handoff and conservative recovery."""
from __future__ import annotations

import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from fastapi import FastAPI

from runner_app.api.auth import Sessions
from runner_app.api.runtime import create_runtime_app
from runner_app.config import DRIVE_INDEX_ID, DRIVE_ROOT_ID, Settings
from runner_app.engine.execution_assembly import build_owner_execution_ports
from runner_app.engine.full_route import ExecutionPorts, make_full_route_factory
from runner_app.engine.integrations import (
    IntegrationConfigError,
    RuntimeIntegrations,
    build_runtime_integrations,
    load_owner_runtime_config,
)
from runner_app.engine.service import Engine
from runner_app.engine.singleton import Singleton
from runner_app.engine.workspace_mailboxes import RouteFactory, WorkspaceMailboxManager
from runner_app.git.imports import ImportGrants
from runner_app.git.registry import SourceRegistry
from runner_app.storage import Database, Paths, open_private_file
from runner_app.storage.ledger import Ledger
from runner_app.storage.run_evidence import RunEvidenceStore


class _DriveBridge(Protocol):
    def close(self) -> None: ...


class Runtime:
    def __init__(self, settings: Settings, paths: Paths, *,
                 import_roots: Sequence[Path] = (),
                 workspace_route_factory: RouteFactory | None = None,
                 execution_ports: ExecutionPorts | None = None) -> None:
        paths.ensure_private()
        self.paths = paths
        self.drive_bridge: _DriveBridge | None = None
        self._singleton = Singleton(paths.state_dir / "runner.lock")
        self._bootstrap_path = paths.token_dir / "session-bootstrap.json"
        try:
            self.ledger = Ledger(Database(paths.database_path),
                                 max_parallel=settings.max_parallel_executions)
            self.ledger.startup()
            owner_config = load_owner_runtime_config(paths)
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
                ) if self.integrations is not None else None
            )
            self.sessions = Sessions()
            bootstrap = self.sessions.issue_bootstrap()
            descriptor = open_private_file(self._bootstrap_path, create=True)
            try:
                content = json.dumps({"bootstrap": bootstrap,
                                      "expires_in_seconds": self.sessions.bootstrap_ttl}).encode()
                os.ftruncate(descriptor, 0)
                with os.fdopen(descriptor, "wb", closefd=False) as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(descriptor)
            finally:
                os.close(descriptor)
            self.app: FastAPI = create_runtime_app(settings, self.engine, self.sessions,
                                                   import_grants=self.import_grants,
                                                   run_evidence=self.run_evidence,
                                                   integration_status=(
                                                       {
                                                           "state": "EXISTING_INSTALLATION_ATTACHED",
                                                           "drive_root_id": self.integrations.attached.config.root_id,
                                                           "drive_index_id": self.integrations.attached.config.index_id,
                                                           "github_login": self.integrations.config.github_login,
                                                           "github_user_id": self.integrations.config.github_user_id,
                                                           "draft_pr_capability": "NOT_PROBED",
                                                           "execution_readiness": self.execution_readiness_code,
                                                       } if self.integrations is not None else None
                                                   ))
            # Uvicorn re-raises SIGTERM after completing its ASGI shutdown. Run cleanup
            # during the shutdown event, before that signal bypasses the CLI finally block.
            self.app.router.add_event_handler("shutdown", self.close)
        except BaseException:
            self._singleton.close()
            raise

    def close(self) -> None:
        try:
            try:
                if self.workspace_mailboxes is not None:
                    self.workspace_mailboxes.close()
                if self.drive_bridge is not None:
                    self.drive_bridge.close()
            finally:
                self.engine.shutdown()
                self._bootstrap_path.unlink(missing_ok=True)
        finally:
            self._singleton.close()
