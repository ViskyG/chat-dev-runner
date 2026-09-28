"""Focused fake-backed proof for the owner-host mailbox maintenance preflight."""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from runner_app.adapters.drive_docs import RegisteredMailbox
from runner_app.adapters.drive_fake import FakeDrive
from runner_app.domain.commands import parse_command

_SCRIPT = (Path(__file__).resolve().parents[1] / "verification" / "implementation"
           / "EP-07" / "read_only_mailbox_preflight.py")
_SPEC = importlib.util.spec_from_file_location("ep07_mailbox_preflight", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
preflight = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(preflight)

_COMMAND_ID = "fake-command-123456"
_RECEIPT_ID = "fake-receipt-123456"


def _command(now: datetime) -> dict[str, object]:
    return {
        "schema_version": "1.0", "request_id": str(uuid4()), "dispatch": "READY",
        "runner_id": "fake-runner", "epoch": str(uuid4()),
        "issued_at": now.isoformat(), "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "action": "CREATE_PROJECT", "project_id": "fake-project", "workspace_id": None,
        "expected_head": None,
        "params": {"name": "Fixture", "source": {"kind": "new"},
                   "template_id": "python-basic", "github_identity": "personal",
                   "visibility": "private"},
        "intent": "private user intent must never appear in preflight output",
    }


def _fixture(command_text: str, receipt_text: str = "\n") -> tuple[FakeDrive, RegisteredMailbox]:
    provider = FakeDrive()
    provider.put_document(_COMMAND_ID, command_text, revision="command-rev")
    provider.put_document(_RECEIPT_ID, receipt_text, revision="receipt-rev")
    return provider, RegisteredMailbox(
        command_id=_COMMAND_ID, parent_id="fake-control-folder-123",
        receipt_id=_RECEIPT_ID,
    )


def test_unexpired_ready_without_matching_terminal_receipt_blocks_maintenance() -> None:
    now = datetime.now(timezone.utc)
    command = _command(now)
    provider, mailbox = _fixture(json.dumps(command), json.dumps({
        "request_id": str(uuid4()), "request_digest": "a" * 64, "status": "COMPLETED",
    }))
    result = preflight.summarize_mailbox(provider, mailbox, scope="root", now=now)
    assert result["maintenance_pending"] is True
    assert result["matching_receipt_status"] is None
    assert result["request_id"] == command["request_id"]
    assert result["expires_at"] == command["expires_at"]
    assert "private user intent" not in json.dumps(result)
    assert provider.calls == [
        ("documents.get", _COMMAND_ID), ("documents.get", _RECEIPT_ID),
        ("documents.get", _COMMAND_ID),
    ]


def test_matching_terminal_receipt_or_expired_ready_is_safe() -> None:
    now = datetime.now(timezone.utc)
    command = _command(now)
    frozen = parse_command(json.dumps(command).encode())
    provider, mailbox = _fixture(json.dumps(command), json.dumps({
        "request_id": command["request_id"], "request_digest": frozen.digest,
        "status": "COMPLETED",
    }))
    result = preflight.summarize_mailbox(provider, mailbox, scope="root", now=now)
    assert result["maintenance_pending"] is False
    assert result["matching_receipt_status"] == "COMPLETED"
    provider.put_document(_RECEIPT_ID, "\n")
    later = now + timedelta(minutes=6)
    result = preflight.summarize_mailbox(provider, mailbox, scope="root", now=later)
    assert result["maintenance_pending"] is False
    assert result["matching_receipt_status"] is None


def test_empty_command_is_safe_but_malformed_command_fails_closed() -> None:
    now = datetime.now(timezone.utc)
    provider, mailbox = _fixture("\n")
    assert preflight.summarize_mailbox(provider, mailbox, scope="root", now=now)[
        "maintenance_pending"
    ] is False
    provider.put_document(_COMMAND_ID, '{"dispatch":"READY"} trailing')
    result = preflight.summarize_mailbox(provider, mailbox, scope="root", now=now)
    assert result["maintenance_pending"] is True
    assert result["command_parse_code"] == "INVALID_JSON"
    assert "trailing" not in json.dumps(result)


def test_command_change_during_preflight_fails_closed() -> None:
    now = datetime.now(timezone.utc)
    provider, mailbox = _fixture(json.dumps(_command(now)))

    def change_after_first_read(fake: FakeDrive, read_count: int) -> None:
        if read_count == 2:
            fake.put_document(_COMMAND_ID, "\n", revision="changed-rev")

    provider.on_document_read[_COMMAND_ID] = change_after_first_read
    with pytest.raises(preflight.PreflightError, match="COMMAND_CHANGED_DURING_PREFLIGHT"):
        preflight.summarize_mailbox(provider, mailbox, scope="root", now=now)


def test_unexpected_provider_failure_does_not_print_details(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    def secret_failure(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("OAuth secret in third-party exception")

    monkeypatch.setattr(preflight, "preflight", secret_failure)
    assert preflight.main([]) == 2
    printed = capsys.readouterr().out
    assert "OAuth secret" not in printed
    assert json.loads(printed)["error"] == "PREFLIGHT_FAILED"


def test_google_provider_uses_request_local_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = object()
    services: list[tuple[str, object]] = []

    def build(name: str, _version: str, **kwargs: object) -> object:
        services.append((name, kwargs["requestBuilder"]))
        return object()

    monkeypatch.setattr(preflight, "isolated_request_builder", lambda _credentials: marker)
    monkeypatch.setattr(preflight, "import_module", lambda _name: SimpleNamespace(build=build))
    preflight._provider(object())
    assert services == [("drive", marker), ("docs", marker)]


def test_workspace_delivery_receipt_is_visible_but_not_terminal_proof() -> None:
    now = datetime.now(timezone.utc)
    command = _command(now)
    receipt = "CHAT-DEV RUNNER / RECEIPT\n" + json.dumps({
        "request_id": command["request_id"], "delivery_status": "DELIVERED",
    }) + "\n"
    provider, mailbox = _fixture(json.dumps(command), receipt)
    result = preflight.summarize_mailbox(provider, mailbox, scope="workspace", now=now)
    assert result["matching_receipt_status"] == "DELIVERED"
    assert result["maintenance_pending"] is True
    assert "receipt_parse_code" not in result
