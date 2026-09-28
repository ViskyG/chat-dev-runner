"""Exact-ID project and workspace mailboxes from completed local provision.

Discovery never searches Drive by title or admits a command. A project mailbox
activates only with terminal receipt publication; a workspace mailbox activates
only with its owner-approved execution route.
"""
from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from runner_app.adapters.drive_docs import FrozenDriveInput, ReadProvider, RegisteredMailbox
from runner_app.adapters.drive_poller import DrivePoller, FrozenSlotStore
from runner_app.adapters.drive_project_receipt import ProjectTerminalReceiptPublisher
from runner_app.adapters.drive_registry import WorkspaceDriveRegistration, WorkspaceDriveRegistry
from runner_app.domain.commands import Provenance
from runner_app.engine.drive_bridge import DrivePollingBridge
from runner_app.engine.service import Engine, Handler
from runner_app.git.registry import SourceRegistry
from runner_app.storage.ledger import Ledger

_LOG = logging.getLogger(__name__)
_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,79}\Z")
RouteFactory = Callable[[WorkspaceDriveRegistration, str, FrozenSlotStore], Handler]


@dataclass(frozen=True)
class WorkspaceRefresh:
    known: int
    activated: int
    new_mailboxes: tuple[str, ...]
    blocked_projects: tuple[str, ...]


class WorkspaceMailboxManager:
    """Register completed, locally managed project workspaces.

    CREATE_PROJECT or CREATE_WORKSPACE can finish after startup. A finite
    background refresh sees each durable completion and loads exact Drive IDs. The
    manager never admits commands itself. Active routes use DrivePoller, which
    freezes one observed command and submits it immediately or returns BUSY.
    """

    def __init__(
        self, ledger: Ledger, sources: SourceRegistry,
        drive_registry: WorkspaceDriveRegistry, provider: ReadProvider,
        engine: Engine, slots_path: Path, template_profiles: Mapping[str, str], *,
        route_factory: RouteFactory | None = None,
        project_receipt: ProjectTerminalReceiptPublisher | None = None,
        refresh_seconds: float = 3.0,
    ) -> None:
        if (ledger.database.path != sources.database.path
                or ledger.database.path != engine.ledger.database.path
                or not 0.25 <= refresh_seconds <= 60.0):
            raise ValueError("Workspace manager requires one Ledger and bounded refresh")
        self.ledger = ledger
        self.sources = sources
        self.drive_registry = drive_registry
        self.provider = provider
        self.engine = engine
        self.slots_path = Path(slots_path)
        self.template_profiles = dict(template_profiles)
        self.route_factory = route_factory
        self.project_receipt = project_receipt
        self.refresh_seconds = refresh_seconds
        self._known: dict[tuple[str, str], RegisteredMailbox] = {}
        self._bridges: dict[tuple[str, str], DrivePollingBridge] = {}
        self._project_known: dict[str, RegisteredMailbox] = {}
        self._project_bridges: dict[str, DrivePollingBridge] = {}
        self._slots: FrozenSlotStore | None = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error_code: str | None = None

    def mailboxes(self) -> dict[tuple[str, str], RegisteredMailbox]:
        with self._lock:
            return dict(self._known)

    def active_count(self) -> int:
        with self._lock:
            return len(self._bridges)

    def project_mailboxes(self) -> dict[str, RegisteredMailbox]:
        with self._lock:
            return dict(self._project_known)

    def project_active_count(self) -> int:
        with self._lock:
            return len(self._project_bridges)

    def _registered(self, project_id: str, workspace_id: str
                    ) -> tuple[WorkspaceDriveRegistration, str] | None:
        project = self.sources.get_project(project_id)
        workspace = self.sources.get_workspace(project_id, workspace_id)
        profile = self.sources.get_workspace_profile(project_id, workspace_id)
        template_id = profile.get("template_id") if profile is not None else None
        profile_id = profile.get("profile_id") if profile is not None else None
        if (project is None or workspace is None or profile is None
                or not isinstance(template_id, str) or not isinstance(profile_id, str)
                or project.get("state") != "READY"
                or workspace.get("state") != "ACTIVE" or workspace.get("archived")
                or self.sources.get_project_remote(project_id) is None
                or self.template_profiles.get(template_id) != profile_id):
            return None
        registration = self.drive_registry.get(project_id, workspace_id)
        if registration.project_id != project_id or registration.workspace_id != workspace_id:
            return None
        return registration, profile_id

    def refresh(self) -> WorkspaceRefresh:
        """Read current durable completions; no provider or local project write."""
        completed = self.ledger.completed_provision_project_ids()
        pairs = {(project_id, "primary") for project_id in completed}
        pairs.update((project_id, workspace_id)
                     for project_id, workspace_id in self.ledger.completed_workspace_pairs()
                     if project_id in completed)
        added: list[str] = []
        blocked: list[str] = []
        for project_id, workspace_id in sorted(pairs):
            key = (project_id, workspace_id)
            with self._lock:
                workspace_ready = self.route_factory is None or key in self._bridges
                project_ready = (workspace_id != "primary" or self.project_receipt is None
                                 or project_id in self._project_bridges)
                if key in self._known and workspace_ready and project_ready:
                    continue
            try:
                candidate = self._registered(project_id, workspace_id)
                if candidate is None:
                    blocked.append(project_id)
                    continue
                registration, profile_id = candidate
                mailbox = registration.mailbox()
                with self._lock:
                    if any(other != key and known.command_id == mailbox.command_id
                           for other, known in self._known.items()):
                        blocked.append(project_id)
                        continue
                    existing = self._known.get(key)
                    if existing is not None and existing != mailbox:
                        blocked.append(project_id)
                        continue
                    if existing is None:
                        self._known[key] = mailbox
                        added.append(mailbox.command_id)
                if self.project_receipt is not None and workspace_id == "primary":
                    project_mailbox = registration.project_mailbox()
                    with self._lock:
                        old = self._project_known.get(project_id)
                        if (old is not None and old != project_mailbox
                                or any(other != project_id
                                       and known.command_id == project_mailbox.command_id
                                       for other, known in self._project_known.items())
                                or any(known.command_id == project_mailbox.command_id
                                       for known in self._known.values())):
                            blocked.append(project_id)
                            continue
                        if old is None:
                            self._project_known[project_id] = project_mailbox
                    if project_id not in self._project_bridges:
                        with self._lock:
                            if self._slots is None:
                                self._slots = FrozenSlotStore(self.slots_path)
                            slots = self._slots
                        project_provenance = Provenance(
                            "drive", project_mailbox.command_id, project_id,
                        )
                        self.engine.register_project_mailbox(project_provenance, profile_id)
                        publisher = self.project_receipt
                        assert publisher is not None

                        def publish(observed: FrozenDriveInput, source: Provenance, *,
                                    _slots: FrozenSlotStore = slots,
                                    _publisher: ProjectTerminalReceiptPublisher = publisher
                                    ) -> None:
                            _publisher.publish_if_terminal(observed, source, _slots)

                        poller = DrivePoller(
                            self.provider, project_mailbox, self.engine, slots,
                            on_observed=publish,
                        )
                        bridge = DrivePollingBridge(poller)
                        with self._lock:
                            self._project_bridges[project_id] = bridge
                            if self._thread is not None and not self._stop.is_set():
                                bridge.start()
                if self.route_factory is not None and key not in self._bridges:
                    with self._lock:
                        if self._slots is None:
                            self._slots = FrozenSlotStore(self.slots_path)
                        slots = self._slots
                    route = self.route_factory(registration, profile_id, slots)
                    provenance = Provenance("drive", mailbox.command_id, project_id, workspace_id)
                    self.engine.register_workspace_route(provenance, profile_id, route)
                    with self._lock:
                        if key not in self._bridges:
                            poller = DrivePoller(self.provider, mailbox, self.engine, slots)
                            bridge = DrivePollingBridge(poller)
                            self._bridges[key] = bridge
                            if self._thread is not None and not self._stop.is_set():
                                bridge.start()
            except Exception as error:  # noqa: BLE001 -- sanitize provider/local details
                code = getattr(error, "code", None)
                self.last_error_code = (
                    code if isinstance(code, str) and _CODE.fullmatch(code)
                    else "WORKSPACE_REGISTRATION_FAILED"
                )
                blocked.append(project_id)
        with self._lock:
            return WorkspaceRefresh(
                known=len(self._known), activated=len(self._bridges),
                new_mailboxes=tuple(added), blocked_projects=tuple(blocked),
            )

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception as error:  # noqa: BLE001 -- no provider text in logs
                code = getattr(error, "code", None)
                self.last_error_code = (
                    code if isinstance(code, str) and _CODE.fullmatch(code)
                    else "WORKSPACE_REFRESH_FAILED"
                )
                _LOG.warning("Workspace mailbox refresh: %s", self.last_error_code)
            self._stop.wait(self.refresh_seconds)

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                raise RuntimeError("WORKSPACE_MANAGER_ALREADY_STARTED")
            # Start previously discovered routes before discovery can add new
            # ones, so a fast refresh cannot start the same bridge twice.
            for bridge in (*self._project_bridges.values(), *self._bridges.values()):
                bridge.start()
            self._thread = threading.Thread(
                target=self._run, name="cdr-workspace-discovery", daemon=True,
            )
            self._thread.start()

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            thread = self._thread
            bridges = [*self._project_bridges.values(), *self._bridges.values()]
        if thread is not None:
            thread.join(timeout=30.0)
        for bridge in bridges:
            bridge.close()
