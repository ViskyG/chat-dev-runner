from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_drive_mailbox_v2 import DOCUMENT, OTHER, TAB, FakeDocs, mailbox, owner_config, tab
from test_github_mailbox_v2 import COMMAND
from test_github_mailbox_v2 import Fixture as GitFixture

import runner_app.engine.docs_v2 as module
from runner_app.adapters.drive_mailbox_v2 import DocsMailboxError, DocsProviderConfig
from runner_app.engine.docs_v2 import DocsPortFactory
from runner_app.engine.intake_v2 import IntakeCoordinator
from runner_app.storage.database import Database
from runner_app.storage.intake_v2 import IntakeStore
from runner_app.storage.paths import Paths


def binding(*, document_id: str = DOCUMENT, tab_id: str = TAB, provider: str = "drive") -> Any:
    # Same structural surface as the owner's config_v2.MailboxConfig dataclass.
    return SimpleNamespace(provider=provider, coordinates={"document_id": document_id, "tab_id": tab_id})


class Harness:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.path, self.value = owner_config(root)
        self.paths = Paths(root / "separate-config", root / "state", root / "cache")
        self.provider = FakeDocs()
        children = self.provider.document["tabs"][0]["childTabs"]
        self.provider.document["tabs"][0] = tab(OTHER, mailbox())
        self.provider.document["tabs"][0]["childTabs"] = children
        self.credentials: list[Any] = []
        self.builds: list[Any] = []
        self.attaches: list[Any] = []
        self.during_auth: Any = None
        self.during_attach: Any = None
        self.roles = {name: SimpleNamespace(file_id=f"legacy-{name}-document")
                      for name in ("root", "installation-index", "command", "state", "receipt")}
        self.attached = SimpleNamespace(index=SimpleNamespace(files=self.roles))

        def auth(paths: Paths, *, refresh: bool = False) -> Any:
            assert refresh is True
            self.credentials.append(paths)
            if self.during_auth:
                self.during_auth()
            return "fake-credentials-never-read-from-file"

        def build(credentials: Any) -> Any:
            self.builds.append(credentials)
            return self.provider

        def attach(config: DocsProviderConfig, provider: Any) -> Any:
            assert provider is self.provider
            self.attaches.append(config)
            if self.during_attach:
                self.during_attach()
            return self.attached

        monkeypatch.setattr(module, "load_authorized_credentials", auth)
        monkeypatch.setattr(module, "_build_provider", build)
        monkeypatch.setattr(DocsProviderConfig, "attach", attach)


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(tmp_path, monkeypatch)


def test_factory_construction_is_inert_and_provider_attachment_reused(harness: Harness) -> None:
    factory = DocsPortFactory(harness.paths)
    assert harness.credentials == harness.builds == harness.attaches == harness.provider.calls == []
    first = factory(harness.path, binding())
    second = factory(harness.path, binding(tab_id=OTHER))
    assert first is not second
    assert first.provider is second.provider is harness.provider
    assert first.read_current().payload == second.read_current().payload == mailbox()
    assert len(harness.credentials) == len(harness.builds) == len(harness.attaches) == 1
    assert harness.credentials[0].config_dir == harness.path.parent
    assert harness.credentials[0].state_dir == harness.paths.state_dir
    assert harness.provider.batches == []


def test_distinct_config_reference_never_reuses_other_cached_provider(harness: Harness) -> None:
    factory = DocsPortFactory(harness.paths)
    factory(harness.path, binding())
    other = harness.path.with_name("another-channel.json")
    other.write_text(json.dumps(harness.value))
    other.chmod(0o600)
    factory(other, binding())
    assert len(harness.credentials) == len(harness.attaches) == 2


def test_invalid_changed_config_never_falls_back_to_old_cached_provider(harness: Harness) -> None:
    factory = DocsPortFactory(harness.paths)
    factory(harness.path, binding())
    changed = {**harness.value, "scope": "wrong-scope"}
    harness.path.write_text(json.dumps(changed))
    with pytest.raises(DocsMailboxError):
        factory(harness.path, binding())
    assert len(harness.credentials) == len(harness.attaches) == 1
    assert harness.provider.batches == []


def test_changed_credential_reference_rebuilds_configuration(harness: Harness, tmp_path: Path) -> None:
    factory = DocsPortFactory(harness.paths)
    factory(harness.path, binding())
    new_root = tmp_path / "another-owner-config"
    new_root.mkdir()
    _, new_value = owner_config(new_root)
    harness.path.write_text(json.dumps(new_value))
    factory(harness.path, binding())
    assert len(harness.credentials) == len(harness.attaches) == 2
    assert harness.credentials[-1].config_dir == Path(new_value["oauth_client_file"]).parent


def test_rotated_token_file_invalidates_cache_without_reading_contents(harness: Harness) -> None:
    factory = DocsPortFactory(harness.paths)
    factory(harness.path, binding())
    Path(harness.value["token_file"]).write_bytes(b"rotated fake credential bytes never parsed")
    factory(harness.path, binding())
    assert len(harness.credentials) == len(harness.attaches) == 2


def test_expected_initial_refresh_does_not_force_repeated_attachment(harness: Harness) -> None:
    factory = DocsPortFactory(harness.paths)
    harness.during_auth = lambda: Path(harness.value["token_file"]).write_bytes(b"fake refreshed token")
    factory(harness.path, binding())
    factory(harness.path, binding())
    assert len(harness.credentials) == len(harness.attaches) == 1


def test_config_changed_during_attachment_is_never_cached(harness: Harness) -> None:
    factory = DocsPortFactory(harness.paths)
    harness.during_attach = lambda: harness.path.write_text(json.dumps(harness.value, indent=4))
    with pytest.raises(DocsMailboxError, match="DOCS_CONFIG_CHANGED_DURING_SETUP"):
        factory(harness.path, binding())
    harness.during_attach = None
    factory(harness.path, binding())
    assert len(harness.credentials) == len(harness.attaches) == 2


@pytest.mark.parametrize("role", ["root", "installation-index", "command", "state", "receipt"])
def test_every_attached_legacy_role_is_protected(harness: Harness, role: str) -> None:
    factory = DocsPortFactory(harness.paths)
    with pytest.raises(DocsMailboxError, match="LEGACY_DOCS_MAILBOX_PROTECTED"):
        factory(harness.path, binding(document_id=harness.roles[role].file_id))
    assert len(harness.attaches) == 1
    assert harness.provider.calls == harness.provider.batches == []


def test_explicit_v1_registry_document_protected_before_any_config_access(harness: Harness) -> None:
    factory = DocsPortFactory(harness.paths, protected_document_ids=["v1-registry-command-document"])
    with pytest.raises(DocsMailboxError, match="LEGACY_DOCS_MAILBOX_PROTECTED"):
        factory(Path("/never-opened-channel.json"), binding(document_id="v1-registry-command-document"))
    assert harness.credentials == harness.builds == harness.attaches == []


def test_non_docs_binding_is_refused_without_preparing_any_provider(harness: Harness) -> None:
    factory = DocsPortFactory(harness.paths)
    with pytest.raises(DocsMailboxError, match="INVALID_DOCS_MAILBOX_BINDING"):
        factory(harness.path, binding(provider="github"))
    assert harness.credentials == harness.attaches == []


def test_concurrent_calls_prepare_once(harness: Harness) -> None:
    factory = DocsPortFactory(harness.paths)
    with ThreadPoolExecutor(max_workers=2) as pool:
        ports = list(pool.map(lambda _: factory(harness.path, binding()), range(2)))
    assert all(port.provider is harness.provider for port in ports)
    assert len(harness.credentials) == len(harness.builds) == len(harness.attaches) == 1
    assert harness.provider.batches == []


def test_slow_mailbox_preflight_does_not_lock_other_docs(harness: Harness, monkeypatch):
    factory = DocsPortFactory(harness.paths)
    factory(harness.path, binding())  # Shared attachment is already established.
    original = module.DocsMailboxPort.read_current
    entered, release = threading.Event(), threading.Event()

    def read(port):
        if port.tab_id == TAB:
            entered.set()
            assert release.wait(3)
        return original(port)

    monkeypatch.setattr(module.DocsMailboxPort, 'read_current', read)
    with ThreadPoolExecutor(max_workers=2) as pool:
        slow = pool.submit(factory, harness.path, binding())
        try:
            assert entered.wait(2)
            fast = pool.submit(factory, harness.path, binding(tab_id=OTHER))
            assert fast.result(timeout=2).tab_id == OTHER
        finally:
            release.set()
        slow.result(timeout=2)


def test_setup_errors_are_sanitized_and_not_cached(harness: Harness) -> None:
    factory = DocsPortFactory(harness.paths)

    def unavailable() -> None:
        raise RuntimeError("fake-secret-provider-detail")

    harness.during_auth = unavailable
    with pytest.raises(DocsMailboxError, match="DOCS_CHANNEL_SETUP_FAILED") as failure:
        factory(harness.path, binding())
    assert "fake-secret-provider-detail" not in str(failure.value)
    harness.during_auth = None
    factory(harness.path, binding())
    assert len(harness.credentials) == 2 and len(harness.attaches) == 1


def test_actual_github_and_docs_ports_share_one_admission_and_execution_claim(
    harness: Harness, tmp_path: Path,
) -> None:
    github_root = tmp_path / "github"
    github_root.mkdir()
    github = GitFixture(github_root)
    common = json.loads(mailbox())
    request_id = "cc333333-3333-4333-8333-333333333333"
    common["request"]["request_id"] = request_id
    common["dispatch"] = "DRAFT"
    draft = json.dumps(common, ensure_ascii=False).encode()
    github.api.advance({COMMAND: draft})
    harness.provider.change(draft)
    docs = DocsPortFactory(harness.paths)(harness.path, binding())
    store = IntakeStore(Database(tmp_path / "cross-channel.sqlite"))
    session = store.start_session("runner-1")
    coordinator = IntakeCoordinator(store, session)
    for name, provider, coordinates in (
        ("github-slot", "github", {"branch": "tested", "path": COMMAND}),
        ("docs-slot", "drive", {"document_id": DOCUMENT, "tab_id": TAB}),
    ):
        store.bind(name, runner_id="runner-1", project_id="project-1", provider=provider,
                   coordinates=coordinates, session_id=session)
    assert coordinator.poll_once("github-slot", github.port).kind == "BASELINED"
    assert coordinator.poll_once("docs-slot", docs).kind == "BASELINED"

    def ready(observed: bytes) -> bytes:
        value = json.loads(observed)
        value["dispatch"] = "READY"
        return json.dumps(value, ensure_ascii=False).encode()

    github.api.advance({COMMAND: ready(github.port.read_current().payload)})
    harness.provider.change(ready(docs.read_current().payload))
    accepted_github = coordinator.poll_once("github-slot", github.port)
    accepted_docs = coordinator.poll_once("docs-slot", docs)
    assert accepted_github.kind == accepted_docs.kind == "ACCEPTED"
    assert accepted_github.request_id == accepted_docs.request_id == request_id
    assert accepted_github.sequence == accepted_docs.sequence == 1
    assert len(store.list_requests()) == 1
    assert json.loads(github.port.read_current().payload)["dispatch"] == "ACCEPTED"
    assert json.loads(docs.read_current().payload)["dispatch"] == "ACCEPTED"

    executions: list[str] = []

    def execute_one() -> Mapping[str, Any] | None:
        request = store.claim_next(session)
        if request is not None:
            executions.append(request["request_id"])
            store.mark_running(request_id, {"executor": "finite-test-callback"})
            store.finish(request_id, status="COMPLETED", result={"callback_executions": 1})
        return request

    assert execute_one() is not None
    assert execute_one() is None
    assert executions == [request_id]
    changed = json.loads(ready(docs.read_current().payload))
    changed["request"]["shell"]["script"] = "different user intent with reused ID"
    harness.provider.change(json.dumps(changed).encode())
    rejected = coordinator.poll_once("docs-slot", docs)
    assert rejected.kind == "REJECTED" and rejected.code == "REQUEST_ID_REUSED"
    assert execute_one() is None and executions == [request_id]
    assert len(store.list_requests()) == 1
    assert json.loads(docs.read_current().payload)["request"] == changed["request"]
    assert github.token_calls == 0
