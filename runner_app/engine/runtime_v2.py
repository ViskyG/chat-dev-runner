"""Optional v2 composition inside the existing single local service."""
from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from runner_app.adapters.github_mailbox_v2 import (
    GitHubMailboxPort,
    GitHubResultTransport,
    GitHubV2Client,
)
from runner_app.config_v2 import MailboxConfig, V2RuntimeConfig
from runner_app.engine.docs_v2 import DocsPortFactory, RecoveringDocsPort
from runner_app.engine.handoff_v2 import HandoffRunner, V2TaskDispatcher
from runner_app.engine.intake_v2 import MailboxPort, notify_terminal
from runner_app.engine.queue_v2 import V2Service
from runner_app.engine.recovery_v2 import recover_interrupted
from runner_app.engine.task_v2 import NativeTaskRunner, TaskProject
from runner_app.executors.compose_v2 import ComposeTaskBackend
from runner_app.executors.rootless_v2 import RootlessError, RootlessTaskBackend
from runner_app.git.argv import run_git
from runner_app.git.github_transport import OwnerTokenFile
from runner_app.git.source_v2 import LocalWorkspace, SourceProject, SourceResolver
from runner_app.storage.intake_v2 import IntakeError, IntakeStore
from runner_app.storage.ledger import Ledger
from runner_app.storage.paths import Paths, ensure_private_directory, open_private_file


def _legacy_document_ids(paths: Paths) -> set[str]:
    """Read local v1 registry identities without provider calls or migration."""
    directory = paths.state_dir / "drive-registrations"
    if not directory.exists() and not directory.is_symlink():
        return set()
    if directory.resolve() != directory:
        raise ValueError("LEGACY_REGISTRY_UNSAFE")
    ensure_private_directory(directory)
    names = list(directory.glob("*.json"))
    if len(names) > 1000:
        raise ValueError("LEGACY_REGISTRY_LIMIT")
    result: set[str] = set()
    for path in names:
        descriptor = open_private_file(path)
        with os.fdopen(descriptor, "rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError("LEGACY_REGISTRY_LIMIT")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise TypeError("LEGACY_REGISTRY_INVALID")
        result.update(item for key, item in value.items()
                      if key.endswith("_doc_id") and isinstance(item, str) and item)
    return result


class V2Assembly:
    def __init__(self, config: V2RuntimeConfig, paths: Paths, ledger: Ledger, *,
                 github_factory: Callable[..., GitHubV2Client] = GitHubV2Client,
                 docs_factory: Callable[[Path, MailboxConfig], MailboxPort] | None = None) -> None:
        self.config, self.paths, self.ledger = config, paths, ledger
        self.store = IntakeStore(ledger.database, queue_limit=config.queue_limit)
        self.session_id = self.store.start_session(config.runner_id)
        self.channel_codes: dict[str, str] = {}
        self.docs_ports: dict[str, RecoveringDocsPort] = {}
        self.backend_codes: dict[str, str] = {}
        self._closed = False
        self._on_recovered: Callable[[], Any] | None = None
        clients: dict[str, GitHubV2Client] = {}
        sources: dict[str, SourceProject] = {}
        projects: dict[str, TaskProject] = {}
        docs_setup_code = "GOOGLE_NOT_CONFIGURED"
        try:
            if docs_factory is None and config.google_config_file is not None:
                try:
                    docs_factory = DocsPortFactory(paths, protected_document_ids=_legacy_document_ids(paths))
                except (OSError, ValueError, TypeError):
                    # A broken optional legacy registry fences Docs only.
                    docs_factory = None
                    docs_setup_code = "LEGACY_REGISTRY_UNAVAILABLE"
            for project_id, project in config.projects.items():
                root = paths.state_dir / "projects-v2" / project_id
                ensure_private_directory(root)
                repo = root / "source.git"
                if not repo.exists() and not repo.is_symlink():
                    run_git(root, ["init", "--bare", str(repo)])
                    repo.chmod(0o700)
                transport = None
                if project.repository is not None and config.github_token_file is not None:
                    client = github_factory(project.repository, OwnerTokenFile(config.github_token_file))
                    clients[project_id] = client
                    transport = GitHubResultTransport(client)
                sources[project_id] = SourceProject(
                    repo, root / "runs",
                    {key: LocalWorkspace(item.path, item.include_dirty, item.include_untracked)
                     for key, item in project.workspaces.items()},
                    fetch_head=transport.fetch_head if transport is not None else None,
                )
                projects[project_id] = TaskProject(repo, root / "runner-results",
                                                   project.native_profiles, transport,
                                                   project.container_profiles)
            bindings: list[tuple[str, MailboxPort]] = []
            for mailbox in config.mailboxes:
                coordinates = dict(mailbox.coordinates)
                port: MailboxPort
                if mailbox.provider == "github":
                    port = GitHubMailboxPort(clients[mailbox.project_id], branch=coordinates["branch"],
                                             command_path=coordinates["path"])
                    repository = config.projects[mailbox.project_id].repository
                    assert repository is not None
                    coordinates["repository_id"] = repository.id
                    coordinates["repository_node_id"] = repository.node_id
                else:
                    if docs_factory is None or config.google_config_file is None:
                        self.channel_codes[mailbox.binding_id] = docs_setup_code
                        continue
                    selected_factory = docs_factory
                    selected_config = config.google_config_file
                    def create_docs(factory: Callable[[Path, MailboxConfig], MailboxPort] = selected_factory,
                                    config_path: Path = selected_config,
                                    binding: MailboxConfig = mailbox) -> MailboxPort:
                        return factory(config_path, binding)

                    port = self.docs_ports[mailbox.binding_id] = RecoveringDocsPort(create_docs)
                self.store.bind(mailbox.binding_id, runner_id=config.runner_id,
                                project_id=mailbox.project_id, provider=mailbox.provider,
                                coordinates=coordinates, session_id=self.session_id)
                bindings.append((mailbox.binding_id, port))
                self.channel_codes[mailbox.binding_id] = "CONFIGURED_NOT_OBSERVED"
            self.resolver = SourceResolver(sources)
            backends: dict[str, RootlessTaskBackend | ComposeTaskBackend] = {}
            def require_running(request_id: str) -> None:
                operation = self.store.get(request_id)
                if operation is None or operation["status"] != "RUNNING":
                    raise IntakeError("OPERATION_NOT_RUNNING")

            if config.rootless_profile_dir is not None:
                try:
                    backends["rootless-docker"] = RootlessTaskBackend(
                        config.rootless_profile_dir, paths.state_dir / "executions-v2",
                        require_running=require_running, record_event=self.store.record_event,
                    )
                    self.backend_codes["rootless-docker"] = "CONFIGURED_NOT_PROBED"
                except (OSError, ValueError, RootlessError) as error:
                    self.backend_codes["rootless-docker"] = getattr(error, "code", "ROOTLESS_UNAVAILABLE")
            if config.compose_profile_dir is not None:
                try:
                    def require_recovering(request_id: str) -> None:
                        if not any(item["request_id"] == request_id for item in
                                   self.store.recovery_candidates(config.runner_id, self.session_id)):
                            raise IntakeError("RECOVERY_AUTHORITY_ENDED")

                    backends["rootless-compose"] = ComposeTaskBackend(
                        config.compose_profile_dir, paths.state_dir / "executions-v2",
                        require_running=require_running, record_event=self.store.record_event,
                        require_recovering=require_recovering,
                    )
                    self.backend_codes["rootless-compose"] = "CONFIGURED_NOT_PROBED"
                except (OSError, ValueError, RootlessError) as error:
                    self.backend_codes["rootless-compose"] = getattr(error, "code", "COMPOSE_UNAVAILABLE")
            self.handler = NativeTaskRunner(self.store, self.resolver, projects, config.native_profiles,
                                            container_backends=backends)
            from runner_app.adapters.handoff_drive import HandoffDriveFactory
            from runner_app.config import DRIVE_INDEX_ID

            protected_ids = {DRIVE_INDEX_ID} | {
                box.coordinates["document_id"] for box in config.mailboxes if box.provider == "drive"}
            handoff_factory: Any = HandoffDriveFactory(paths)
            try:
                protected_ids.update(_legacy_document_ids(paths))
            except (OSError, ValueError, TypeError):
                handoff_factory = None
            protected = frozenset(protected_ids)
            self.handoff = HandoffRunner(self.store, config, paths,
                                         provider_factory=handoff_factory,
                                         protected_document_ids=protected, native=self.handler)
            self.dispatcher = V2TaskDispatcher(self.handler, self.handoff)
            self.service = V2Service(self.store, self.session_id, bindings, self.dispatcher,
                                     intake_enabled=lambda: bool(ledger.state()["armed"]),
                                     poll_seconds=config.poll_seconds,
                                     terminal_observer=self.dispatcher.publish_state,
                                     startup_recovery=self._recover)
        except BaseException:
            self.store.end_session(self.session_id)
            raise

    @property
    def configured(self) -> bool:
        return bool(self.service.bindings)

    def _recover(self) -> None:
        recover_interrupted(self.handler, self.config.runner_id, self.session_id)
        for operation in self.store.restart_notifications(self.config.runner_id, self.session_id):
            if operation["project_id"] in self.handler.projects:
                notify_terminal(self.store, self.dispatcher.publish_state, operation["request_id"])
        if not self._closed and self._on_recovered is not None:
            self._on_recovered()

    def status(self) -> dict[str, Any]:
        channels = {**self.channel_codes, **self.service.last_states}
        for binding_id, port in self.docs_ports.items():
            if port.last_error:
                channels[binding_id] = port.last_error
        return {"protocol": "cdr-runtime/2", "runner_id": self.config.runner_id,
                "session_id": self.session_id, "armed": self.ledger.state()["armed"],
                "configured": self.configured,
                "channels": channels,
                "projects": list(self.config.projects), "queue_limit": self.config.queue_limit,
                "native_profiles": list(self.config.native_profiles),
                "container_backends": dict(self.backend_codes),
                "handoff_targets": {project.project_id: {
                    key: {"workspace_id": target.workspace_id,
                          "allowed_drive_parents": sorted(target.allowed_drive_parents),
                          "result_document_id": target.result_document_id,
                          "result_tab_id": target.result_tab_id,
                          "relative_inbox": ".runner/handoffs"}
                    for key, target in project.handoff_targets.items()}
                    for project in self.config.projects.values() if project.handoff_targets},
                "handoff_protocol": "cdr-handoff/1",
                "capability_status": "IMPLEMENTED_NOT_QUALIFIED"}

    def start(self, *, on_recovered: Callable[[], Any] | None = None) -> None:
        self._on_recovered = on_recovered
        self.service.start()

    def request_stop(self) -> None:
        self.service.request_stop()

    def close(self, *, deadline: float | None = None) -> None:
        if self._closed:
            return
        self._closed = True
        self.service.close(deadline=deadline if deadline is not None else time.monotonic() + 20)

    def attach_api(self, app: FastAPI) -> None:
        # Existing runtime middleware authenticates all paths except its exact
        # health/session/static allowlist, including these additive v2 routes.
        @app.get("/api/v2/status")
        def status() -> dict[str, Any]:
            return self.status()

        @app.get("/api/v2/requests")
        def requests() -> dict[str, Any]:
            return {"requests": self.store.list_requests()}

        @app.get("/api/v2/requests/{request_id}")
        def request(request_id: str) -> Any:
            result = self.store.get(request_id)
            return result if result else JSONResponse({"error": "REQUEST_NOT_FOUND"}, status_code=404)
