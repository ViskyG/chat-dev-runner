"""Exact-ID workspace mailbox discovery from completed local provision records.

Discovery is read-only. It never searches Drive by title or freezes a command.
Without an executable owner-approved route, registrations remain inert and no
workspace polling thread or DELIVER_VERIFY_PUBLISH capability is installed.
"""
from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from runner_app.adapters.drive_docs import ReadProvider, RegisteredMailbox
from runner_app.adapters.drive_poller import DrivePoller, FrozenSlotStore
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
    """Register only completed, locally managed primary workspaces.

    CREATE_PROJECT can finish after startup. A finite background refresh sees
    its new durable completion and loads the exact registered Drive IDs. The
    manager never admits commands itself. Active routes use DrivePoller, which
    freezes one observed command and submits it immediately or returns BUSY.
    """

    def __init__(
        self, ledger: Ledger, sources: SourceRegistry,
        drive_registry: WorkspaceDriveRegistry, provider: ReadProvider,
        engine: Engine, slots_path: Path, template_profiles: Mapping[str, str], *,
        route_factory: RouteFactory | None = None, refresh_seconds: float = 3.0,
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
        self.refresh_seconds = refresh_seconds
        self._known: dict[tuple[str, str], RegisteredMailbox] = {}
        self._bridges: dict[tuple[str, str], DrivePollingBridge] = {}
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

    def _registered(self, project_id: str) -> tuple[WorkspaceDriveRegistration, str] | None:
        project = self.sources.get_project(project_id)
        workspace = self.sources.get_workspace(project_id, "primary")
        profile = self.sources.get_workspace_profile(project_id, "primary")
        template_id = profile.get("template_id") if profile is not None else None
        profile_id = profile.get("profile_id") if profile is not None else None
        if (project is None or workspace is None or profile is None
                or not isinstance(template_id, str) or not isinstance(profile_id, str)
                or project.get("state") != "READY"
                or workspace.get("state") != "ACTIVE" or workspace.get("archived")
                or self.sources.get_project_remote(project_id) is None
                or self.template_profiles.get(template_id) != profile_id):
            return None
        registration = self.drive_registry.get(project_id, "primary")
        if registration.project_id != project_id or registration.workspace_id != "primary":
            return None
        return registration, profile_id

    def refresh(self) -> WorkspaceRefresh:
        """Read current durable completions; no provider or local project write."""
        completed = self.ledger.completed_provision_project_ids()
        added: list[str] = []
        blocked: list[str] = []
        for project_id in sorted(completed):
            key = (project_id, "primary")
            with self._lock:
                if key in self._known and (
                    self.route_factory is None or key in self._bridges
                ):
                    continue
            try:
                candidate = self._registered(project_id)
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
                if self.route_factory is None:
                    continue
                with self._lock:
                    if self._slots is None:
                        self._slots = FrozenSlotStore(self.slots_path)
                    slots = self._slots
                route = self.route_factory(registration, profile_id, slots)
                provenance = Provenance("drive", mailbox.command_id, project_id, "primary")
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
            self._thread = threading.Thread(
                target=self._run, name="cdr-workspace-discovery", daemon=True,
            )
            self._thread.start()
            for bridge in self._bridges.values():
                bridge.start()

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            thread = self._thread
            bridges = list(self._bridges.values())
        if thread is not None:
            thread.join(timeout=30.0)
        for bridge in bridges:
            bridge.close()
