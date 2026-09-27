"""Explicit local process startup, private session handoff and conservative recovery."""
from __future__ import annotations

import json
import os
from collections.abc import Sequence
from pathlib import Path

from fastapi import FastAPI

from runner_app.api.auth import Sessions
from runner_app.api.runtime import create_runtime_app
from runner_app.config import Settings
from runner_app.engine.service import Engine
from runner_app.engine.singleton import Singleton
from runner_app.git.imports import ImportGrants
from runner_app.git.registry import SourceRegistry
from runner_app.storage import Database, Paths, open_private_file
from runner_app.storage.ledger import Ledger
from runner_app.storage.run_evidence import RunEvidenceStore


class Runtime:
    def __init__(self, settings: Settings, paths: Paths, *,
                 import_roots: Sequence[Path] = ()) -> None:
        paths.ensure_private()
        self._singleton = Singleton(paths.state_dir / "runner.lock")
        self._bootstrap_path = paths.token_dir / "session-bootstrap.json"
        try:
            self.ledger = Ledger(Database(paths.database_path),
                                 max_parallel=settings.max_parallel_executions)
            self.ledger.startup()
            self.import_grants = ImportGrants(paths.state_dir / "import-handles",
                                              roots=import_roots)
            self.source_registry = SourceRegistry(self.ledger.database,
                                                  paths.state_dir / "managed")
            self.run_evidence = RunEvidenceStore(self.ledger.database, self.source_registry,
                                                 paths.evidence_dir)
            self.engine = Engine(self.ledger, import_grants=self.import_grants)
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
                                                   run_evidence=self.run_evidence)
            # Uvicorn re-raises SIGTERM after completing its ASGI shutdown. Run cleanup
            # during the shutdown event, before that signal bypasses the CLI finally block.
            self.app.router.add_event_handler("shutdown", self.close)
        except BaseException:
            self._singleton.close()
            raise

    def close(self) -> None:
        try:
            self.engine.shutdown()
            self._bootstrap_path.unlink(missing_ok=True)
        finally:
            self._singleton.close()
