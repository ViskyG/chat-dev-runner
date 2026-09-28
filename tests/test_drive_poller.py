"""Offline known-mailbox poll bridge tests; never access a Google account."""

from __future__ import annotations

import base64
import json
import stat
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from runner_app.adapters.drive_docs import RegisteredMailbox
from runner_app.adapters.drive_fake import FakeDrive
from runner_app.adapters.drive_poller import DrivePoller, FrozenSlotStore, PollError, PollSchedule
from runner_app.domain.commands import LocalPolicy, Provenance
from runner_app.engine.drive_bridge import DrivePollingBridge
from runner_app.engine.service import Engine, OperationResult
from runner_app.storage import Database
from runner_app.storage.ledger import Ledger

COMMAND_ID = "fake-poller-command-123456"
PARENT_ID = "fake-poller-workspace-12345"
HEAD = "a" * 40


def command(epoch: str, *, request_id: str | None = None, intent: str = "one") -> dict:
    now = datetime.now(timezone.utc)
    return {
        "schema_version": "1.0", "request_id": request_id or str(uuid4()),
        "dispatch": "READY", "runner_id": "local-runner", "epoch": epoch,
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "action": "VERIFY", "project_id": "project", "workspace_id": "workspace",
        "expected_head": HEAD, "params": {"profile_id": "python-quick"}, "intent": intent,
    }


def fixture(tmp_path: Path, *, handler=None) -> tuple[DrivePoller, FakeDrive, Engine, Ledger]:
    ledger = Ledger(Database(tmp_path / "engine.sqlite"), max_parallel=1)
    ledger.startup()
    ledger.arm()
    provider = FakeDrive()
    provider.put_file({
        "id": COMMAND_ID, "name": "COMMAND",
        "mimeType": "application/vnd.google-apps.document",
        "parents": [PARENT_ID], "trashed": False, "isAppAuthorized": True,
    })
    provider.put_document(COMMAND_ID, json.dumps(command(ledger.state()["epoch"])),
                          revision="revision-1")
    mailbox = RegisteredMailbox(
        command_id=COMMAND_ID, parent_id=PARENT_ID,
        project_id="project", workspace_id="workspace",
        changeset_id="fake-poller-changeset-123",
    )
    provenance = Provenance("drive", COMMAND_ID, "project", "workspace")
    policy = LocalPolicy(
        mailboxes={COMMAND_ID: provenance},
        approved_profiles=frozenset({"python-quick"}),
        project_ids=frozenset({"project"}),
        workspace_pairs=frozenset({("project", "workspace")}),
    )
    engine = Engine(ledger, policy=policy,
                    handlers={"VERIFY": handler or (lambda *_: OperationResult())})
    poller = DrivePoller(provider, mailbox, engine, FrozenSlotStore(tmp_path / "frozen"))
    return poller, provider, engine, ledger


def test_duplicate_poll_has_one_admission_and_private_durable_freeze(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(frozen, _context):
        calls.append(frozen.digest)
        return OperationResult()

    poller, _, engine, ledger = fixture(tmp_path, handler=handler)
    try:
        first = poller.poll_once()
        assert first.kind == "SUBMITTED"
        assert first.admission is not None and first.admission.execute
        second = poller.poll_once()
        assert second.kind == "ALREADY_OBSERVED"
        assert second.admission is None
        assert second.request_id == first.request_id
        deadline = time.monotonic() + 3
        while ledger.get(first.request_id)["status"] == "RUNNING" and time.monotonic() < deadline:
            time.sleep(0.005)
        assert len(calls) == 1
        frozen = tmp_path / "frozen" / f"{first.request_id}.frozen.json"
        admission = tmp_path / "frozen" / f"{first.request_id}.admission.json"
        assert stat.S_IMODE(frozen.stat().st_mode) == 0o600
        assert stat.S_IMODE(admission.stat().st_mode) == 0o600
        assert stat.S_IMODE(frozen.parent.stat().st_mode) == 0o700
        payload = json.loads(frozen.read_text())
        assert payload["command"]["revision"] == "revision-1"
        assert payload["source"]["command_id"] == COMMAND_ID
        assert payload["command"]["raw_base64"]
    finally:
        engine.shutdown()


def test_same_request_id_changed_body_is_never_re_admitted(tmp_path: Path) -> None:
    poller, provider, engine, _ = fixture(tmp_path)
    try:
        first = poller.poll_once()
        changed = command(engine.ledger.state()["epoch"], request_id=first.request_id,
                          intent="changed same request")
        provider.put_document(COMMAND_ID, json.dumps(changed), revision="revision-2")
        with pytest.raises(PollError) as caught:
            poller.poll_once()
        assert caught.value.code == "REQUEST_ID_REUSED"
    finally:
        engine.shutdown()


def test_busy_is_terminal_for_poll_and_not_replayed_when_capacity_frees(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def handler(frozen, _context):
        calls.append(frozen.command["request_id"])
        entered.set()
        release.wait(2)
        return OperationResult()

    poller, provider, engine, _ = fixture(tmp_path, handler=handler)
    try:
        first = poller.poll_once()
        assert entered.wait(1)
        second_command = command(engine.ledger.state()["epoch"])
        provider.put_document(COMMAND_ID, json.dumps(second_command), revision="revision-2")
        second = poller.poll_once()
        assert second.kind == "SUBMITTED"
        assert second.admission is not None
        assert second.admission.receipt["code"] == "BUSY"
        assert not second.admission.execute
        release.set()
        assert poller.poll_once().kind == "ALREADY_OBSERVED"
        assert calls == [first.request_id]
    finally:
        release.set()
        engine.shutdown()


def test_old_epoch_is_refused_once_without_auto_replay(tmp_path: Path) -> None:
    poller, provider, engine, ledger = fixture(tmp_path)
    try:
        old = command(str(uuid4()))
        provider.put_document(COMMAND_ID, json.dumps(old), revision="revision-old")
        first = poller.poll_once()
        assert first.admission is not None
        assert first.admission.receipt["code"] == "WRONG_RUNNER_EPOCH"
        assert not first.admission.execute
        assert poller.poll_once().kind == "ALREADY_OBSERVED"
        assert ledger.get(old["request_id"])["status"] == "REJECTED"
    finally:
        engine.shutdown()


def test_crash_after_freeze_never_auto_replays(tmp_path: Path) -> None:
    poller, _, engine, ledger = fixture(tmp_path)
    original_submit = engine.submit

    def interrupted(*_args):
        raise RuntimeError("simulated interruption before durable admission result")

    engine.submit = interrupted
    try:
        with pytest.raises(RuntimeError):
            poller.poll_once()
        engine.submit = original_submit
        resumed = DrivePoller(poller.provider, poller.mailbox, engine,
                              FrozenSlotStore(tmp_path / "frozen"))
        result = resumed.poll_once()
        assert result.kind == "NEEDS_RECONCILIATION"
        assert ledger.get(result.request_id) is None
    finally:
        engine.submit = original_submit
        engine.shutdown()


def test_poll_schedule_backs_off_and_does_not_mark_failed_read_successful() -> None:
    schedule = PollSchedule()
    assert schedule.due(0)
    schedule = schedule.success(100)
    assert schedule.last_success == 100
    assert not schedule.due(114)
    assert schedule.due(115)
    failed = schedule.failure(115, jitter=0.5)
    assert failed.last_success == 100
    assert failed.next_due == 152.5
    assert not failed.due(152)
    assert failed.due(152.5)
    for index in range(20):
        failed = failed.failure(200 + index * 400, jitter=1)
    assert failed.failures == 16
    assert failed.next_due <= 200 + 19 * 400 + 300
    assert failed.success(failed.next_due).failures == 0



def test_serving_bridge_never_freezes_disarmed_command(tmp_path: Path) -> None:
    poller, provider, engine, ledger = fixture(tmp_path)
    bridge = DrivePollingBridge(poller)
    try:
        ledger.disarm()
        provider.calls.clear()
        assert bridge.poll_once_if_due().kind == "DISARMED"
        assert provider.calls == []
        assert list((tmp_path / "frozen").iterdir()) == []

        ledger.arm()
        ready = command(ledger.state()["epoch"])
        provider.put_document(COMMAND_ID, json.dumps(ready), revision="revision-armed")
        observed = bridge.poll_once_if_due()
        assert observed.kind == "SUBMITTED"
        assert observed.request_id == ready["request_id"]
        assert ledger.get(ready["request_id"]) is not None
        assert bridge.poll_once_if_due().kind == "WAITING"
    finally:
        bridge.close()
        engine.shutdown()


def test_bridge_does_not_admit_until_system_state_is_published(tmp_path: Path) -> None:
    poller, provider, engine, ledger = fixture(tmp_path)
    provider.calls.clear()
    now = [0.0]
    attempts = []

    def publish(state):
        attempts.append(state["armed"])
        if len(attempts) == 1:
            raise PollError("STATE_WRITE_UNCERTAIN")

    bridge = DrivePollingBridge(poller, state_publisher=publish, clock=lambda: now[0])
    try:
        first = bridge.poll_once_if_due()
        assert first.kind == "ERROR" and first.code == "STATE_WRITE_UNCERTAIN"
        assert provider.calls == []
        assert ledger.list_operations()["items"] == []
        assert bridge.poll_once_if_due().kind == "WAITING"

        now[0] = 40.0
        second = bridge.poll_once_if_due()
        assert second.kind == "SUBMITTED"
        assert attempts == [True, True]
        assert provider.calls
        assert ledger.get(second.request_id) is not None
    finally:
        bridge.close()
        engine.shutdown()


def test_bridge_republishes_capability_state_after_route_registration(tmp_path: Path) -> None:
    poller, _, engine, _ = fixture(tmp_path)
    now = [0.0]
    advertised = []
    bridge = DrivePollingBridge(
        poller,
        state_publisher=lambda _state: advertised.append(
            (engine.capabilities(), sorted(engine.policy.approved_profiles))),
        clock=lambda: now[0],
    )
    original_capabilities = engine.capabilities
    try:
        assert bridge.poll_once_if_due().kind == "SUBMITTED"
        assert advertised == [(["VERIFY"], ["python-quick"])]
        engine.capabilities = lambda: ["DELIVER_VERIFY_PUBLISH", "VERIFY"]
        now[0] = 1.0
        assert bridge.poll_once_if_due().kind == "WAITING"
        assert advertised == [
            (["VERIFY"], ["python-quick"]),
            (["DELIVER_VERIFY_PUBLISH", "VERIFY"], ["python-quick"]),
        ]
        engine.policy = replace(
            engine.policy,
            approved_profiles=frozenset({"python-quick", "second-profile"}),
        )
        now[0] = 2.0
        assert bridge.poll_once_if_due().kind == "WAITING"
        assert advertised[-1] == (
            ["DELIVER_VERIFY_PUBLISH", "VERIFY"],
            ["python-quick", "second-profile"],
        )
    finally:
        engine.capabilities = original_capabilities
        bridge.close()
        engine.shutdown()


def test_unregistered_mailbox_is_refused_before_polling(tmp_path: Path) -> None:
    poller, provider, engine, _ = fixture(tmp_path)
    try:
        mailbox = RegisteredMailbox("different-known-command-123", PARENT_ID)
        with pytest.raises(PollError) as caught:
            DrivePoller(provider, mailbox, engine, poller.slots)
        assert caught.value.code == "MAILBOX_NOT_REGISTERED"
        assert provider.calls == []
    finally:
        engine.shutdown()


def test_load_frozen_rechecks_exact_mailbox_scope_and_bytes(tmp_path: Path) -> None:
    poller, _, engine, _ = fixture(tmp_path)
    try:
        result = poller.poll_once()
        loaded = poller.slots.load_frozen(result.request_id, poller.provenance)
        original = (tmp_path / "frozen" / f"{result.request_id}.frozen.json").read_bytes()
        encoded = json.loads(original)
        assert loaded.raw_command == base64.b64decode(encoded["command"]["raw_base64"])
        assert loaded.command_revision == "revision-1"
        assert loaded.command_text_sha256 == encoded["command"]["sha256"]
        assert loaded.command.digest == encoded["command"]["digest"]
        assert loaded.raw_changeset is None
        with pytest.raises(PollError) as error:
            poller.slots.load_frozen(
                result.request_id, Provenance("drive", "another-registered-command",
                                              "project", "workspace"),
            )
        assert error.value.code == "FROZEN_SOURCE_MISMATCH"
    finally:
        engine.shutdown()


def test_load_frozen_revalidates_changeset_bytes_revision_and_plan(tmp_path: Path) -> None:
    from runner_app.adapters.drive_docs import read_ready_snapshot

    poller, provider, engine, _ = fixture(tmp_path)
    changeset_id = poller.mailbox.changeset_id
    assert changeset_id is not None
    changeset = {
        "schema_version": "1.0", "change_id": str(uuid4()),
        "project_id": "project", "workspace_id": "workspace", "base_head": HEAD,
        "files": [{"op": "upsert", "path": "src/hello.py", "expected_sha256": None,
                   "content_utf8": "print('hello')\n"}],
    }
    provider.put_file({"id": changeset_id, "name": "CHANGESET",
                       "mimeType": "application/vnd.google-apps.document",
                       "parents": [PARENT_ID], "trashed": False, "isAppAuthorized": True})
    provider.put_document(changeset_id, json.dumps(changeset), revision="change-rev-1")
    delivered = command(engine.ledger.state()["epoch"])
    delivered["action"] = "DELIVER_VERIFY_PUBLISH"
    delivered["params"] = {"profile_id": "python-quick", "changeset_doc_id": changeset_id,
                            "changeset_revision": "change-rev-1", "publication": "never"}
    provider.put_document(COMMAND_ID, json.dumps(delivered), revision="command-rev-2")
    try:
        snapshot = read_ready_snapshot(provider, poller.mailbox)
        assert snapshot is not None
        request_id, created = poller.slots.freeze(snapshot, poller.provenance)
        assert created
        loaded = poller.slots.load_frozen(request_id, poller.provenance)
        assert loaded.raw_command == snapshot.raw_command
        assert loaded.raw_changeset == snapshot.raw_changeset
        assert loaded.changeset_revision == "change-rev-1"
        assert loaded.change_plan == snapshot.change_plan

        path = tmp_path / "frozen" / f"{request_id}.frozen.json"
        payload = json.loads(path.read_bytes())
        payload["changeset"]["revision"] = "a forged revision"
        path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        with pytest.raises(PollError) as error:
            poller.slots.load_frozen(request_id, poller.provenance)
        assert error.value.code == "FROZEN_SLOT_INVALID"
    finally:
        engine.shutdown()


@pytest.mark.parametrize("attack", ["symlink", "hardlink", "truncated"])
def test_frozen_slot_rejects_unsafe_or_partial_file(tmp_path: Path, attack: str) -> None:
    poller, _, engine, _ = fixture(tmp_path)
    try:
        first = poller.poll_once()
        frozen = tmp_path / "frozen" / f"{first.request_id}.frozen.json"
        if attack == "symlink":
            target = tmp_path / "target.json"
            target.write_bytes(frozen.read_bytes())
            frozen.unlink()
            frozen.symlink_to(target)
        elif attack == "hardlink":
            (tmp_path / "frozen" / "extra-link").hardlink_to(frozen)
        else:
            frozen.write_bytes(frozen.read_bytes()[:20])
        with pytest.raises(PollError) as error:
            poller.slots.load_frozen(first.request_id, poller.provenance)
        assert error.value.code in {"FROZEN_SLOT_UNSAFE", "FROZEN_SLOT_INVALID"}
    finally:
        engine.shutdown()


def test_directory_replacement_is_refused_without_creating_new_slot(tmp_path: Path) -> None:
    poller, _, engine, _ = fixture(tmp_path)
    old = tmp_path / "frozen"
    moved = tmp_path / "moved"
    old.rename(moved)
    old.mkdir(mode=0o700)
    try:
        with pytest.raises(PollError) as error:
            poller.poll_once()
        assert error.value.code == "FROZEN_STORE_NOT_PRIVATE"
        assert list(old.iterdir()) == []
    finally:
        engine.shutdown()


def test_published_freeze_without_admission_marker_requires_reconciliation(tmp_path: Path,
                                                                              monkeypatch) -> None:
    import runner_app.adapters.drive_poller as poller_module

    poller, _, engine, ledger = fixture(tmp_path)
    original_fsync = poller_module.os.fsync

    def interrupted_fsync(descriptor: int) -> None:
        if stat.S_ISDIR(poller_module.os.fstat(descriptor).st_mode):
            raise OSError("simulated crash before directory sync")
        original_fsync(descriptor)

    monkeypatch.setattr(poller_module.os, "fsync", interrupted_fsync)
    try:
        with pytest.raises(PollError) as error:
            poller.poll_once()
        assert error.value.code == "FROZEN_SLOT_IO"
        monkeypatch.setattr(poller_module.os, "fsync", original_fsync)
        resumed = DrivePoller(poller.provider, poller.mailbox, engine,
                              FrozenSlotStore(tmp_path / "frozen"))
        result = resumed.poll_once()
        assert result.kind == "NEEDS_RECONCILIATION"
        assert ledger.get(result.request_id) is None
        assert not (tmp_path / "frozen" / f"{result.request_id}.admission.json").exists()
    finally:
        monkeypatch.setattr(poller_module.os, "fsync", original_fsync)
        engine.shutdown()


def test_dispatch_fault_is_recorded_once_without_replaying_frozen_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    poller, _, engine, ledger = fixture(tmp_path)

    def fail_start(_thread: threading.Thread) -> None:
        raise RuntimeError("simulated worker start failure")

    monkeypatch.setattr("runner_app.engine.service.threading.Thread.start", fail_start)
    try:
        first = poller.poll_once()
        assert first.admission is not None
        assert first.admission.receipt["status"] == "FAULT"
        assert first.admission.receipt["code"] == "DISPATCH_FAILED"
        assert poller.poll_once().kind == "ALREADY_OBSERVED"
        assert ledger.get(first.request_id)["status"] == "FAULT"
    finally:
        engine.shutdown()
